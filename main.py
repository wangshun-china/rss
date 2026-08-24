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
from sources import reddit, twitter, twitter_synd
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
    """拉取源集合，返回 {key: items}；RSS_SOURCES 环境变量可限定只跑 twitter 或 reddit。"""
    buckets = {}
    scope = os.environ.get("RSS_SOURCES", "all")  # all / twitter / reddit

    api_key = os.environ.get("TWITTER_API_KEY")
    if not api_key:
        log.info("未配置 TWITTER_API_KEY，仅使用 syndication 免费通道")

    filters = cfg.get("filters", {})
    skip_reply = filters.get("twitter_skip_replies", False)
    skip_rt = filters.get("twitter_skip_retweets", False)

    if scope in ("all", "twitter"):
        import time as _time

        for name in cfg.get("twitter_accounts", []):
            key = f"twitter:{name}"
            # 主通道：syndication 嵌入接口（免费）；备用：twitterapi.io
            tweets = None
            try:
                tweets = twitter_synd.fetch_user_tweets(name)
            except Exception as e:
                log.warning("syndication 拉 @%s 失败: %s", name, e)
                if api_key:
                    try:
                        tweets = twitter.fetch_user_tweets(api_key, name)
                    except Exception as e2:
                        log.warning("twitterapi.io 拉 @%s 也失败: %s", name, e2)
            if tweets is None:
                continue
            buckets[key] = [
                t for t in tweets
                if not (skip_reply and t["is_reply"])
                and not (skip_rt and t["type"] == "retweet")
            ]
            _time.sleep(3)  # 轻微间隔，避免触发上游风控

    if scope not in ("all", "reddit"):
        return buckets

    cid = os.environ.get("REDDIT_CLIENT_ID") or None
    csec = os.environ.get("REDDIT_CLIENT_SECRET") or None
    for sub in cfg.get("reddit_subreddits", []):
        key = f"reddit:{sub}"
        try:
            buckets[key] = reddit.fetch_subreddit(sub, client_id=cid, client_secret=csec)
        except Exception as e:
            log.warning("拉取 r/%s 失败: %s", sub, e)

    return buckets


def split_new(store_data, key, items):
    """返回 (未推送过的条目列表, 是否首运行)。"""
    seen = set(store_data.get(key, []))
    fresh = [it for it in items if str(it["id"]) not in seen]
    return fresh, key not in store_data


def build_tweet_card(name, items, fmt, sort_key, max_items, text_max):
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
        lines.insert(0, f"**AI 总结**：{summary}")
    return build_card(f"X · @{name} · {len(items)} 条更新", "blue", lines)


def build_reddit_card(sub, items, fmt, sort_key, max_items):
    lines = []
    for p in sorted(items, key=sort_key)[:max_items]:
        author = f" · u/{p['author']}" if p["author"] else ""
        lines.append(
            f"**[{truncate(p['title'], 120)}]({p['url']})**{author} · {fmt(p['time'])}"
        )
    return build_card(f"Reddit · r/{sub} · {len(items)} 个新帖", "orange", lines)


def run(dry_run=False):
    load_env()
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
    buckets = collect(cfg)

    pending = []      # (key, ids, card)
    baselines = {}    # 首运行只记基线不推送
    total_new = 0

    for key in sorted(buckets):
        items = buckets[key]
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
            build_tweet_card(name, fresh, fmt, sort_key, max_items, text_max)
            if source == "twitter"
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
