"""入口：拉取 X / Reddit 增量 -> 组装飞书卡片 -> 推送 -> 回写去重状态。

用法：
    python main.py            # 正式运行（需要 .env 或环境变量）
    python main.py --dry-run  # 只打印将推送的内容，不真正发送
"""

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import yaml

import ai
from feishu import build_card, send
from sources import hn, reddit, twitter_synd
import store

log = logging.getLogger("rss")
BASE = os.path.dirname(os.path.abspath(__file__))


def load_env():
    """极简 .env 加载：不覆盖已存在的环境变量。"""
    path = os.path.join(BASE, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


def make_times(offset_hours):
    """返回 (fmt, sort_key)：fmt 把各源的时间字段转成展示文本，sort_key 用于按时间排序。"""
    tz = timezone(timedelta(hours=offset_hours))

    def parse(value):
        if value is None:
            return None
        if isinstance(value, (int, float)):  # reddit OAuth 的 epoch 秒
            return datetime.fromtimestamp(value, tz=timezone.utc)
        try:
            return parsedate_to_datetime(str(value))
        except Exception:
            pass
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None

    def fmt(value):
        dt = parse(value)
        if dt is None:
            return ""
        return dt.astimezone(tz).strftime("%m-%d %H:%M")

    def sort_key(item):
        dt = parse(item.get("time"))
        return dt.timestamp() if dt else 0.0

    return fmt, sort_key


def truncate(text, limit):
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def collect(cfg):
    """拉取源集合，返回 (buckets, x_stats)；RSS_SOURCES 环境变量可限定范围
    （逗号分隔组合：twitter / reddit / hn，默认全部）。"""
    buckets = {}
    scope = os.environ.get("RSS_SOURCES", "all")
    scopes = set(scope.split(",")) if scope != "all" else {"twitter", "reddit", "hn"}
    x_stats = {"attempted": 0, "failed": []}

    filters = cfg.get("filters", {})
    skip_reply = filters.get("twitter_skip_replies", False)
    skip_rt = filters.get("twitter_skip_retweets", False)

    if "twitter" in scopes:
        import time as _time

        api_key = os.environ.get("TWITTER_API_KEY")
        for name in cfg.get("twitter_accounts", []):
            key = f"twitter:{name}"
            x_stats["attempted"] += 1
            try:
                tweets, profile = twitter_synd.fetch_user_tweets(name)
            except Exception as e:
                log.warning("syndication 拉 @%s 失败: %s", name, e)
                x_stats["failed"].append(name)
                continue
            buckets[key] = {
                "tweets": [
                    t for t in tweets
                    if not (skip_reply and t["is_reply"])
                    and not (skip_rt and t["type"] == "retweet")
                ],
                "profile": profile,
            }
            _time.sleep(3)  # 轻微间隔，避免触发上游风控

    if "reddit" in scopes:
        cid = os.environ.get("REDDIT_CLIENT_ID") or None
        csec = os.environ.get("REDDIT_CLIENT_SECRET") or None
        for sub in cfg.get("reddit_subreddits", []):
            key = f"reddit:{sub}"
            try:
                buckets[key] = reddit.fetch_subreddit(sub, client_id=cid, client_secret=csec)
            except Exception as e:
                log.warning("拉取 r/%s 失败: %s", sub, e)

    if "hn" in scopes:
        try:
            buckets["hn:front"] = hn.fetch_front(limit=15)
        except Exception as e:
            log.warning("拉取 HN 失败: %s", e)

    return buckets, x_stats


def split_new(store_data, key, items):
    """返回 (未推送过的条目列表, 是否首运行)。"""
    seen = set(store_data.get(key, []))
    fresh = [it for it in items if str(it["id"]) not in seen]
    return fresh, key not in store_data


def build_tweet_card(name, items, profile, fmt, sort_key, max_items, text_max):
    ordered = sorted(items, key=sort_key)[:15]

    ai_result = None
    if ai.enabled():
        try:
            ai_result = ai.translate_and_summarize(ordered)
            log.info("@%s AI 处理完成（翻译 %d 条）", name, len(ai_result["translations"]))
        except Exception as e:
            log.warning("@%s AI 增强失败，按原文推送: %s", name, e)

    trans = (ai_result or {}).get("translations") or {}
    lines = []

    # 作者简介行：让人一眼知道这个账号是干什么的
    if profile:
        head = f"**{profile['name']}** · {profile['followers']:,} 粉丝"
        if profile["bio"]:
            head += f"\n{profile['bio']}"
        lines.append(head)
        insert_at = 1
    else:
        insert_at = 0

    for i, t in enumerate(ordered[:max_items]):
        meta = fmt(t["time"])
        if t["is_reply"]:
            meta += f" · 回复 @{t['reply_to']}"
        elif t["type"] == "retweet":
            meta += " · 转推"
        body = truncate(t["text"], text_max)
        zh = trans.get(i)
        if zh:
            body += f"\n译：{zh}"
        lines.append(
            f"**{meta}**　[原文]({t['url']})\n{body}\n💙 {t['likes']}"
        )
    summary = (ai_result or {}).get("summary")
    if summary:
        lines.insert(insert_at, f"**AI 总结**：{summary}")
    return build_card(f"X · @{name} · {len(items)} 条更新", "blue", lines)


def build_reddit_card(sub, items, fmt, sort_key, max_items):
    import time as _time
    from sources import reddit as _reddit

    ordered = sorted(items, key=sort_key)[:max_items]

    # 补充帖子正文（间隔防限流；拿不到就只推标题）
    for p in ordered:
        detail = _reddit.fetch_post_detail(p["url"])
        if detail:
            p["body"] = detail["selftext"]
            p["score"] = detail["score"]
            p["comments"] = detail["num_comments"]
        _time.sleep(0.5)

    # AI 输入：标题 + 正文前 800 字
    ai_items = [{"i": i,
                 "text": f"{p['title']}\n{truncate(p.get('body') or '', 800)}"}
                for i, p in enumerate(ordered)]

    ai_result = None
    if ai.enabled() and any(x["text"].strip() for x in ai_items):
        try:
            ai_result = ai.translate_and_summarize(ai_items)
            log.info("r/%s AI 处理完成（翻译 %d 条）",
                     sub, len(ai_result["translations"]))
        except Exception as e:
            log.warning("r/%s AI 增强失败: %s", sub, e)
    trans = (ai_result or {}).get("translations") or {}
    summary = (ai_result or {}).get("summary")

    lines = []
    for i, p in enumerate(ordered):
        author = f" · u/{p['author']}" if p["author"] else ""
        score = f" · 👍 {p.get('score', 0)} 💬 {p.get('comments', 0)}"
        head = (f"**[{truncate(p['title'], 120)}]({p['url']})**"
                f"{author}{score} · {fmt(p['time'])}")
        block = head
        body_text = (p.get("body") or "").strip()
        if body_text:
            block += f"\n原文：{truncate(body_text, 2500)}"
        zh = trans.get(i)
        if zh:
            block += f"\n译：{zh}"
        lines.append(block)

    if summary:
        lines.insert(0, f"**AI 总结**：{summary}")
    return build_card(f"Reddit · r/{sub} · {len(items)} 个新帖", "orange", lines)


def build_hn_card(items):
    ai_items = [{"text": p["title"]} for p in items]

    ai_result = None
    if ai.enabled():
        try:
            ai_result = ai.translate_and_summarize(ai_items)
            log.info("HN AI 处理完成（翻译 %d 条）", len(ai_result["translations"]))
        except Exception as e:
            log.warning("HN AI 增强失败: %s", e)
    trans = (ai_result or {}).get("translations") or {}
    summary = (ai_result or {}).get("summary")

    lines = []
    for i, p in enumerate(items):
        line = (f"**[{truncate(p['title'], 150)}]({p['url']})** · "
                f"👍 {p['points']} 💬 {p['comments']} · [讨论]({p['hn_url']})")
        zh = trans.get(i)
        if zh:
            line += f"\n译：{zh}"
        lines.append(line)

    if summary:
        lines.insert(0, f"**AI 总结**：{summary}")
    return build_card(f"Hacker News · 首页精选 · {len(items)} 条", "yellow", lines)


def run(dry_run=False):
    load_env()
    scope = os.environ.get("RSS_SOURCES", "all")  # all / twitter / reddit
    with open(os.path.join(BASE, "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    push_cfg = cfg.get("push", {})
    max_items = int(push_cfg.get("max_items_per_card", 10))
    text_max = int(push_cfg.get("tweet_text_max_chars", 500))
    fmt, sort_key = make_times(int(cfg.get("timezone_offset_hours", 8)))

    webhook = os.environ.get("FEISHU_WEBHOOK")
    secret = os.environ.get("FEISHU_SECRET") or None
    if not dry_run and not webhook:
        raise SystemExit("缺少 FEISHU_WEBHOOK（.env 或 GitHub Secrets）")

    store_data = store.load()
    buckets, x_stats = collect(cfg)

    # X 拉取连续整轮全失败时发告警卡片（第 4 轮触发一次，不重复刷屏）
    if scope in ("all", "twitter") and cfg.get("twitter_accounts"):
        streak_key = "_twitter_fail_streak"
        prev = int(store_data.get(streak_key) or 0)
        if x_stats["attempted"] == 0:
            cur = prev
        elif len(x_stats["failed"]) == x_stats["attempted"]:
            cur = prev + 1
        else:
            cur = 0
        store_data[streak_key] = cur
        if cur >= 4 > prev and not dry_run:
            names = "、".join(f"@{n}" for n in x_stats["failed"]) or "全部账号"
            try:
                send(webhook, secret, build_card(
                    "X 订阅拉取异常",
                    "red",
                    [f"已连续 {cur} 轮全部账号拉取失败：{names}\n"
                     f"推文不会永久丢失（恢复后会自动补拉），但请检查服务器代理与接口可用性。"],
                ))
                log.warning("已发送 X 拉取异常告警（连续失败 %d 轮）", cur)
            except Exception as e:
                log.warning("告警卡片发送失败: %s", e)

    pending = []      # (key, ids, card)
    baselines = {}    # 首运行只记基线不推送
    total_new = 0

    for key in sorted(buckets):
        bucket = buckets[key]
        # twitter 源带作者档案，reddit 源是纯列表
        if isinstance(bucket, dict) and "tweets" in bucket:
            items, profile = bucket["tweets"], bucket["profile"]
        else:
            items, profile = bucket, None
        fresh, first_run = split_new(store_data, key, items)
        if first_run:
            log.info("[%s] 首次运行，记录 %d 条基线，本次不推送", key, len(items))
            baselines[key] = [str(it["id"]) for it in items]
            continue
        if not fresh:
            log.info("[%s] 无新内容", key)
            continue
        total_new += len(fresh)
        source, name = key.split(":", 1)
        card = (
            build_tweet_card(name, fresh, profile, fmt, sort_key, max_items, text_max)
            if source == "twitter"
            else build_hn_card(fresh)
            if source == "hn"
            else build_reddit_card(name, fresh, fmt, sort_key, max_items)
        )
        if len(fresh) > max_items:
            card["elements"].append(
                {"tag": "note", "elements": [{"tag": "plain_text",
                                              "content": f"另有 {len(fresh) - max_items} 条未展示"}]}
            )
        pending.append((key, [str(it["id"]) for it in fresh], card))

    # 发送阶段：成功才记录为已推送，失败留给下轮重试
    sent, failed = 0, 0
    for key, ids, card in pending:
        if dry_run:
            import json as _json
            print(f"\n===== DRY-RUN 将推送 [{key}] =====")
            print(_json.dumps(card, ensure_ascii=False, indent=2)[:3000])
            store.record(store_data, key, ids)
            sent += 1
            continue
        try:
            send(webhook, secret, card)
            store.record(store_data, key, ids)
            sent += 1
            log.info("[%s] 已推送 %d 条", key, len(ids))
        except Exception as e:
            failed += 1
            log.error("[%s] 推送失败（下轮重试）: %s", key, e)

    store_data.update(baselines)
    store.save(store_data)

    log.info(
        "完成：%d 个源有新内容（%d 条），推送成功 %d 个源，失败 %d，基线初始化 %d",
        len(pending), total_new, sent, failed, len(baselines),
    )
    return 1 if failed else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(run(dry_run="--dry-run" in sys.argv))
