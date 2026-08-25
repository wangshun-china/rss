"""X 增量轮询器：advanced_search + since_id 游标。

每小时调用一次合并查询（5 个账号 OR 在一起），配合 since_id 只取上次
之后的新推文——不为重复的旧内容买单。结果按作者分组生成飞书卡片
（带作者简介与 AI 中文翻译/总结）。

游标存于 state.json 的 "_x_since_id"。首次运行无游标时先做一次基线
拉取（只记游标不推送）。不做启动回补：重启窗口期的漏推按需求接受。
推文正文不截断。
"""

import html as htmllib
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests
import yaml

os.environ.setdefault("RSS_DATA_DIR", "/data")
sys.path.insert(0, "/app")

import ai  # noqa: E402
import store  # noqa: E402
from feishu import build_card, send  # noqa: E402

log = logging.getLogger("poller")

API_BASE = "https://api.twitterapi.io"
API_KEY = os.environ.get("TWITTER_API_KEY") or ""
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK") or ""
FEISHU_SECRET = os.environ.get("FEISHU_SECRET") or None
INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS") or 3600)
MAX_PAGES = 3
TZ = timezone(timedelta(hours=int(os.environ.get("TIMEZONE_OFFSET_HOURS") or 8)))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def load_accounts():
    with open("/app/config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg.get("twitter_accounts") or []


# ---------- 解析 ----------

def pick(d, *keys, default=""):
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return default


def _username_from_url(url):
    try:
        return urlparse(url).path.strip("/").split("/")[0]
    except Exception:
        return ""


def parse_tweet(t):
    """兼容多种字段形态；作者名优先从 url 提取（部分投递不含 user 对象）。"""
    tid = str(pick(t, "id_str", "id", "rest_id", default=""))
    text = str(pick(t, "text", "full_text", default="")).strip()
    if not tid or not text:
        return None
    url = pick(t, "url", "twitterUrl",
               default=f"https://x.com/i/status/{tid}")
    u = t.get("user") or t.get("author") or {}
    created = pick(t, "createdAt", "created_at", default=None)
    likes = int(pick(t, "favoriteCount", "likeCount", "like_count",
                     "favorite_count", default=0) or 0)
    is_reply = bool(pick(t, "isReply", "inReplyToStatusIdStr",
                         "in_reply_to_status_id_str", "inReplyToStatusId",
                         default=False))
    reply_to = str(pick(t, "inReplyToUsername", "inReplyToScreenName",
                        "in_reply_to_screen_name", default=""))
    is_rt = text.startswith("RT @")
    return {
        "id": tid,
        "text": text,
        "url": url,
        "time": created,
        "type": "retweet" if is_rt else "tweet",
        "is_reply": is_reply,
        "reply_to": reply_to,
        "likes": likes,
        "_username": u.get("screen_name") or u.get("userName")
                     or _username_from_url(url),
        "_profile": {
            "name": htmllib.unescape(u.get("name") or ""),
            "description": u.get("description") or "",
            "followers": int(u.get("followers_count")
                             or u.get("followers") or 0),
        },
    }


def fmt_time(value):
    if not value:
        return ""
    dt = None
    if isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value / 1000 if value > 1e12 else value,
                                    tz=timezone.utc)
    else:
        try:
            dt = parsedate_to_datetime(str(value))
        except Exception:
            pass
        if dt is None:
            try:
                dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except Exception:
                return str(value)[:16]
    return dt.astimezone(TZ).strftime("%m-%d %H:%M")


def truncate(text, limit):
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def snowflake_to_ms(tid):
    try:
        return (int(tid) >> 22) + 1288834974657
    except Exception:
        return 0


# ---------- API ----------

def fetch_page(query, cursor=""):
    params = {"query": query, "queryType": "Latest"}
    if cursor:
        params["cursor"] = cursor
    r = requests.get(f"{API_BASE}/twitter/tweet/advanced_search",
                     headers={"X-API-Key": API_KEY},
                     params=params, timeout=30)
    r.raise_for_status()
    body = r.json()
    return body.get("tweets") or [], bool(body.get("has_next_page")), \
        body.get("next_cursor") or ""


def fetch_new_tweets(query, since_id):
    """带 since_id 的查询，翻页收集（最多 MAX_PAGES 页）。"""
    q = query + (f" since_id:{since_id}" if since_id else "")
    all_tweets, cursor = [], ""
    for _ in range(MAX_PAGES):
        raws, has_next_page, next_cursor = fetch_page(q, cursor)
        for x in raws:
            t = parse_tweet(x)
            if t:
                all_tweets.append(t)
        if not has_next_page or not next_cursor:
            break
        cursor = next_cursor
        time.sleep(2)
    return all_tweets


def fetch_baseline(query):
    """基线拉取：只取第一页用于确定游标起点，不推送。"""
    raws, _, _ = fetch_page(query)
    return [t for t in (parse_tweet(x) for x in raws) if t]


# ---------- 卡片与推送 ----------

def push_author(username, tweets, profile):
    tweets = sorted(tweets, key=lambda t: snowflake_to_ms(t["id"]))
    lines = []
    insert_at = 0
    if profile and (profile["name"] or profile["bio"]):
        head = f"**{profile['name']}** · {profile['followers']:,} 粉丝"
        if profile["bio"]:
            head += f"\n{truncate(profile['bio'], 160)}"
        lines.append(head)
        insert_at = 1

    ai_result = None
    if ai.enabled():
        try:
            ai_result = ai.translate_and_summarize(tweets[:15])
            log.info("@%s AI 处理完成（翻译 %d 条）",
                     username, len(ai_result["translations"]))
        except Exception as e:
            log.warning("@%s AI 增强失败: %s", username, e)
    trans = (ai_result or {}).get("translations") or {}

    for i, t in enumerate(tweets[:10]):
        meta = fmt_time(t["time"])
        if t["is_reply"]:
            meta += f" · 回复 @{t['reply_to']}"
        elif t["type"] == "retweet":
            meta += " · 转推"
        zh = trans.get(i)
        body = t["text"] + (f"\n译：{zh}" if zh else "")
        lines.append(f"**{meta}**　[原文]({t['url']})\n{body}\n💙 {t['likes']}")

    summary = (ai_result or {}).get("summary")
    if summary:
        lines.insert(insert_at, f"**AI 总结**：{summary}")

    card = build_card(f"X · @{username} · {len(tweets)} 条更新", "blue", lines)
    send(FEISHU_WEBHOOK, FEISHU_SECRET, card)


# ---------- 主循环 ----------

def run_cycle(accounts):
    state = store.load()
    since_id = str(state.get("_x_since_id") or "")
    baseline = not since_id

    query = " OR ".join(f"from:{a}" for a in accounts) + " since:2026-08-21"

    if baseline:
        tweets = fetch_baseline(query)
        if not tweets:
            log.warning("[基线] 查询无结果，游标未建立")
            return
        max_id = max(tweets, key=lambda t: int(t["id"]))["id"]
        state["_x_since_id"] = max_id
        store.save(state)
        log.info("[基线] 已记录游标 %s（%d 条），本次不推送", max_id, len(tweets))
        return

    tweets = fetch_new_tweets(query, since_id)
    if not tweets:
        log.info("本轮无新推文")
        return

    groups, profiles = {}, {}
    for t in tweets:
        g = groups.setdefault(t["_username"], [])
        g.append(t)
        p = t["_profile"]
        name = t["_username"]
        if name not in profiles and (p["name"] or p["description"]):
            profiles[name] = {
                "name": p["name"] or f"@{name}",
                "screen_name": name,
                "bio": p["description"],
                "followers": p["followers"],
            }

    store_data = store.load()
    any_fail = False
    pushed_total = 0
    for username, items in sorted(groups.items()):
        key = f"twitter:{username}"
        seen = set(store_data.get(key, []))
        fresh = [t for t in items if t["id"] not in seen]
        if not fresh:
            continue
        try:
            push_author(username, fresh, profiles.get(username))
            store.record(store_data, key, [t["id"] for t in fresh])
            store.save(store_data)
            pushed_total += len(fresh)
            log.info("@%s 已推送 %d 条", username, len(fresh))
        except Exception as e:
            any_fail = True
            log.error("@%s 飞书推送失败: %s", username, e)

    # 无推送失败就推进游标（含"全部已见"的空转轮次）；有失败则不推进，下轮自动重试
    if not any_fail:
        state = store.load()
        max_id = max([int(t["id"]) for t in tweets]
                     + [int(state.get("_x_since_id") or 0)])
        state["_x_since_id"] = str(max_id)
        store.save(state)
        log.info("游标推进至 %s，本轮推送 %d 条", max_id, pushed_total)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    accounts = load_accounts()
    if not accounts:
        raise SystemExit("config.yaml 未配置 twitter_accounts")
    if not API_KEY:
        raise SystemExit("缺少 TWITTER_API_KEY")

    log.info("增量轮询启动：%d 个账号，间隔 %ds", len(accounts), INTERVAL_SECONDS)
    while True:
        started = time.time()
        try:
            run_cycle(accounts)
        except Exception:
            log.exception("本轮轮询异常（下轮自动重试）")
        rest = max(60, INTERVAL_SECONDS - (time.time() - started))
        log.info("休眠 %.0f 秒后进行下一轮", rest)
        time.sleep(rest)


if __name__ == "__main__":
    main()
