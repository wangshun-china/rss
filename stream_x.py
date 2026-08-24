"""X 实时流客户端：通过 twitterapi.io WebSocket 接收五个账号的新推文。

设计：
- 启动时确保服务端存在一条激活的过滤规则（from: 五个账号）
- 可选启动回补（BACKFILL_ON_START=1）：用 REST 拉一次最新推文补上停机期间的缺口
- 常驻 WebSocket：实时接收匹配推文，缓冲聚合后按账号推送飞书卡片
- 断线自动重连（官方要求断开后至少等 90 秒）
- 去重状态复用 data/state.json，与旧轮询格式一致
"""

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests
import yaml
import websocket

os.environ.setdefault("RSS_DATA_DIR", "/data")
sys.path.insert(0, "/app")

import ai  # noqa: E402
import store  # noqa: E402
from feishu import build_card, send  # noqa: E402

log = logging.getLogger("stream")

API_BASE = "https://api.twitterapi.io"
WS_URL = "wss://ws.twitterapi.io/twitter/tweet/websocket"
RULE_TAG = "rss-push-x"

API_KEY = os.environ.get("TWITTER_API_KEY") or ""
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK") or ""
FEISHU_SECRET = os.environ.get("FEISHU_SECRET") or None
FLUSH_SECONDS = int(os.environ.get("X_FLUSH_SECONDS") or 120)
BACKFILL = os.environ.get("BACKFILL_ON_START", "1") == "1"

TZ = timezone(timedelta(hours=int(os.environ.get("TIMEZONE_OFFSET_HOURS") or 8)))

buffer_lock = threading.Lock()
buffer = {}  # username -> {"tweets": [...], "profile": {...}}
last_activity = time.time()


def load_accounts():
    with open("/app/config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg.get("twitter_accounts") or []


def api(method, path, payload=None):
    r = requests.request(
        method, API_BASE + path,
        headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
        json=payload, timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _rule_value(accounts):
    """构造规则值：账号 + since 日期过滤，确保旧推文（置顶/热门/精选）
    在匹配阶段就被排除，不会投递计费。窗口与客户端 72h 龄期过滤对齐。"""
    since = (datetime.now(timezone.utc) - timedelta(hours=72)).strftime("%Y-%m-%d")
    return " OR ".join(f"from:{a}" for a in accounts) + f" since:{since}"


def ensure_rule(accounts):
    """保证服务端存在一条覆盖全部账号的激活规则；返回是否新建/有变更。"""
    value = _rule_value(accounts)
    rules = api("GET", "/oapi/tweet_filter/get_rules").get("rules") or []
    mine = next((r for r in rules if r.get("tag") == RULE_TAG), None)

    if mine and mine.get("value") == value:
        needs_update = (int(mine.get("is_effect") or 0) != 1
                        or float(mine.get("interval_seconds") or 0) != 1800)
        if needs_update:
            api("POST", "/oapi/tweet_filter/update_rule",
                {"rule_id": mine["rule_id"], "tag": RULE_TAG, "value": value,
                 "interval_seconds": 1800, "is_effect": 1})
            log.info("规则已激活/更新（interval=1800s）")
        else:
            log.info("规则已存在且生效，无需变更")
        return False

    if mine:
        api("POST", "/oapi/tweet_filter/update_rule",
            {"rule_id": mine["rule_id"], "tag": RULE_TAG, "value": value,
             "interval_seconds": 1800, "is_effect": 1})
        log.info("规则已更新并激活")
        return True

    created = api("POST", "/oapi/tweet_filter/add_rule",
                  {"tag": RULE_TAG, "value": value, "interval_seconds": 1800})
    rule_id = (created.get("rules") or [{}])[0].get("rule_id") or created.get("rule_id")
    api("POST", "/oapi/tweet_filter/update_rule",
        {"rule_id": rule_id, "tag": RULE_TAG, "value": value,
         "interval_seconds": 1800, "is_effect": 1})
    log.info("规则已创建并激活")
    return True


# ---------- 推文字段解析（兼容多种返回形态） ----------

def pick(d, *keys, default=""):
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return default


def parse_tweet(t):
    tid = str(pick(t, "id_str", "id", "rest_id", default=""))
    text = str(pick(t, "text", "full_text", default="")).strip()
    if not tid or not text:
        return None
    u = t.get("user") or t.get("author") or {}
    if isinstance(u, dict) and isinstance(u.get("legacy"), dict):
        legacy = u["legacy"]
        profile_src = {**{k: u.get(k) for k in ("name", "screen_name")}, **legacy}
    else:
        profile_src = u
    created = pick(t, "createdAt", "created_at", default=None)
    likes = int(pick(t, "favoriteCount", "like_count", "favorite_count", default=0) or 0)
    is_reply = bool(pick(t, "inReplyToStatusIdStr", "in_reply_to_status_id_str",
                         "inReplyToStatusId", default=False))
    reply_to = str(pick(t, "inReplyToScreenName", "in_reply_to_screen_name", default=""))
    is_rt = text.startswith("RT @")
    return {
        "id": tid,
        "text": text,
        "url": f"https://x.com/{profile_src.get('screen_name') or 'i'}/status/{tid}",
        "time": created,
        "type": "retweet" if is_rt else "tweet",
        "is_reply": is_reply,
        "reply_to": reply_to,
        "likes": likes,
        "_user": {
            "name": profile_src.get("name") or "",
            "screen_name": profile_src.get("screen_name") or "",
            "description": profile_src.get("description") or "",
            "followers": int(profile_src.get("followers_count")
                             or profile_src.get("followers") or 0),
        },
    }


def fmt_time(value):
    if not value:
        return ""
    dt = None
    if isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value / 1000 if value > 1e12 else value, tz=timezone.utc)
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


def truncate(text, limit=500):
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


# ---------- 缓冲与推送 ----------

def flush():
    with buffer_lock:
        if not buffer:
            return
        batches = buffer
        buffer.clear()

    for username, data in batches.items():
        tweets = sorted(data["tweets"], key=lambda t: str(t["time"]))
        profile = data["profile"]
        lines = []
        if profile:
            head = f"**{profile['name']}** · {profile['followers']:,} 粉丝"
            if profile["bio"]:
                head += f"\n{profile['bio']}"
            lines.append(head)
            insert_at = 1
        else:
            insert_at = 0

        ai_result = None
        if ai.enabled():
            try:
                ai_result = ai.translate_and_summarize(tweets[:15])
            except Exception as e:
                log.warning("@%s AI 增强失败: %s", username, e)
        trans = (ai_result or {}).get("translations") or {}

        for i, t in enumerate(tweets[:10]):
            meta = fmt_time(t["time"])
            if t["is_reply"]:
                meta += f" · 回复 @{t['reply_to']}"
            elif t["type"] == "retweet":
                meta += " · 转推"
            body = truncate(t["text"])
            zh = trans.get(i)
            if zh:
                body += f"\n译：{zh}"
            lines.append(f"**{meta}**　[原文]({t['url']})\n{body}\n💙 {t['likes']}")

        summary = (ai_result or {}).get("summary")
        if summary:
            lines.insert(insert_at, f"**AI 总结**：{summary}")

        card = build_card(f"X · @{username} · {len(tweets)} 条更新", "blue", lines)
        try:
            send(FEISHU_WEBHOOK, FEISHU_SECRET, card)
            # 只有真正推出去的才记录为已见，失败留给下次
            store_data = store.load()
            store.record(store_data, f"twitter:{username}",
                         [t["id"] for t in tweets])
            store.save(store_data)
            log.info("@%s 已推送 %d 条", username, len(tweets))
        except Exception as e:
            log.error("@%s 飞书推送失败（未记录，将重试）: %s", username, e)


def buffer_flusher():
    global last_activity
    while True:
        time.sleep(15)
        with buffer_lock:
            empty = not buffer
        if empty:
            continue
        if time.time() - last_activity >= FLUSH_SECONDS:
            flush()


def handle_tweet_event(payload):
    global last_activity
    tweets_in = payload.get("tweets") or []
    with buffer_lock:
        for raw in tweets_in:
            t = parse_tweet(raw)
            if not t:
                continue
            user_info = t.pop("_user")
            username = user_info.get("screen_name") or ""
            seen = set(store.load().get(f"twitter:{username}", []))
            if t["id"] in seen:
                log.info("[去重] @%s 重复推文 %s，丢弃", username, t["id"])
                continue
            bucket = buffer.setdefault(username, {"tweets": [], "profile": None})
            if bucket["profile"] is None:
                bucket["profile"] = {
                    "name": user_info["name"] or f"@{username}",
                    "screen_name": username,
                    "bio": truncate(user_info["description"], 160),
                    "followers": user_info["followers"],
                }
            bucket["tweets"].append(t)
            last_activity = time.time()


# ---------- 启动回补 ----------

def backfill(api_key, accounts):
    """用 REST 拉各账号最新推文，把停机期间漏掉的推出去（旧->新顺序）。"""
    from main import build_tweet_card  # 复用既有卡片构建（含 AI）
    store_data = store.load()
    for name in accounts:
        key = f"twitter:{name}"
        try:
            resp = requests.get(
                "https://api.twitterapi.io/twitter/user/last_tweets",
                headers={"X-API-Key": api_key},
                params={"userName": name},
                timeout=30,
            )
            resp.raise_for_status()
            raw = (resp.json().get("data") or {}).get("tweets") or []
            tweets = [t for t in (parse_tweet(x) for x in raw) if t]
        except Exception as e:
            log.warning("回补 @%s 失败: %s", name, e)
            continue

        seen = set(store_data.get(key, []))
        fresh = [t for t in reversed(tweets) if t["id"] not in seen]  # 旧->新
        if not fresh:
            log.info("[回补 @%s] 无缺口", name)
            continue
        u = fresh[0]["_user"]
        profile = {"name": u["name"] or f"@{name}", "screen_name": name,
                   "bio": truncate(u["description"], 160),
                   "followers": u["followers"]}
        try:
            card = build_tweet_card(name, fresh, profile,
                                    lambda v: "", lambda x: 0, 10, 500)
            send(FEISHU_WEBHOOK, FEISHU_SECRET, card)
            store.record(store_data, key, [t["id"] for t in fresh])
            store.save(store_data)
            log.info("[回补 @%s] 补推 %d 条", name, len(fresh))
        except Exception as e:
            log.error("[回补 @%s] 推送失败: %s", name, e)


# ---------- 主流程 ----------

def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    accounts = load_accounts()
    if not accounts:
        raise SystemExit("config.yaml 未配置 twitter_accounts")

    if not API_KEY:
        raise SystemExit("缺少 TWITTER_API_KEY")

    ensure_rule(accounts)
    if BACKFILL:
        backfill(API_KEY, accounts)

    threading.Thread(target=buffer_flusher, daemon=True).start()

    def on_open(ws):
        log.info("WebSocket 已连接")

    def on_message(ws, message):
        try:
            payload = json.loads(message)
        except Exception:
            return
        et = payload.get("event_type")
        if et == "tweet":
            handle_tweet_event(payload)
            with buffer_lock:
                total = sum(len(b["tweets"]) for b in buffer.values())
            if total >= 8:
                flush()
        elif et == "connected":
            log.info("流握手成功")
        # ping 事件忽略

    def on_error(ws, err):
        log.warning("WS 错误: %s", err)

    def on_close(ws, code, reason):
        log.warning("WS 关闭 code=%s reason=%s，95 秒后重连", code, reason)

    while True:
        ws = websocket.WebSocketApp(
            WS_URL,
            header={"x-api-key": API_KEY},
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        try:
            ws.run_forever(ping_interval=40, ping_timeout=30)
        except Exception as e:
            log.error("run_forever 异常: %s", e)
        try:
            flush()  # 断线前把缓冲推干净
        except Exception:
            pass
        time.sleep(95)  # 官方要求：重连前至少等 90 秒


if __name__ == "__main__":
    main()
