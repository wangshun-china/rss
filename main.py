"""入口：拉取 Reddit/HN/通用 RSS -> 组装飞书卡片 -> 推送 -> 回写去重状态。

用法：
    python main.py            # 正式运行（需要 .env 或环境变量）
    python main.py --dry-run  # 只打印将推送的内容，不真正发送、不写状态
"""

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import yaml

log = logging.getLogger("rss")
BASE = os.path.dirname(os.path.abspath(__file__))

ALL_SCOPES = {"reddit", "hn", "rss", "trending", "radar", "papers", "deals"}

# 羊毛判定 risk 标签 -> 中文提示
_DEAL_RISK = {"phone": "需手机号", "card": "需绑卡/实名", "time-limited": "限时活动"}


def _deal_note(judged):
    """把判定结果转成展示用的一行说明。"""
    text = judged.get("note") or ""
    risk = _DEAL_RISK.get(judged.get("risk"))
    if risk:
        text = f"{text}（{risk}）" if text else risk
    return f"🎟 {text}" if text else ""


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


# 必须先于 ai/sources 导入执行：这些模块在导入期读取代理与 AI 配置
load_env()

import ai  # noqa: E402
from feishu import build_card, send  # noqa: E402
from sources import deals, generic_rss, github_trending, hn, hf_models, hf_papers, reddit  # noqa: E402
import store  # noqa: E402


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


def per_item_caps(n, budget=22000):
    """按条数均摊整卡文本预算（飞书单卡 30KB 上限，扣除标题/分隔/备注开销）。

    正文按英文≈1字节/字、译文按中文≈3字节/字估算，返回 (body_cap, zh_cap)。
    条数越少单条预算越大：单帖长文可推到 ~7300 字并带全量译文，10 条满载则收紧。
    """
    per = max(1200, budget // max(1, n))
    return per // 3, per // 5


# 正文不超过该长度时原文+全文翻译直接展示；更长则只展示 AI 提炼的要点，
# 细节通过标题链接看原文——长文塞卡片里没法读，靠摘要保证不丢关键信息
SHORT_BODY = 600


def prepare_ai_text(body):
    """AI 输入：短文全量；长文取前 2500 字并标注要点模式（提示词据此输出要点而非全译）。"""
    body = (body or "").strip()
    if not body:
        return ""
    if len(body) <= SHORT_BODY:
        return body
    return "（长文，请输出要点）" + truncate(body, 2500)


def parse_scope():
    """RSS_SOURCES 环境变量 -> 源集合（逗号分隔组合，默认全部）。"""
    scope = os.environ.get("RSS_SOURCES", "all")
    return set(scope.split(",")) if scope != "all" else set(ALL_SCOPES)


def collect(cfg):
    """拉取源集合，返回 (buckets, stats)。

    stats 记录各范围 attempted/failed，供 run() 在范围内全部源失败时以非零码
    退出触发告警；stats["drops"] 记录被相关性过滤剔除的条目 id（run() 会把
    它们记入去重状态，避免每小时反复重判同一批被过滤内容）。
    """
    buckets = {}
    scopes = parse_scope()
    stats = {name: {"attempted": 0, "failed": 0}
             for name in ("hn", "rss", "trending", "radar", "papers", "deals")}
    stats["reddit"] = {"attempted": 0, "failed": []}
    drops = {}

    if "reddit" in scopes:
        cid = os.environ.get("REDDIT_CLIENT_ID") or None
        csec = os.environ.get("REDDIT_CLIENT_SECRET") or None
        listing = cfg.get("reddit_listing") or "hot"
        for sub in cfg.get("reddit_subreddits", []):
            key = f"reddit:{sub}"
            stats["reddit"]["attempted"] += 1
            try:
                buckets[key] = reddit.fetch_subreddit(
                    sub, client_id=cid, client_secret=csec, listing=listing)
            except Exception as e:
                stats["reddit"]["failed"].append(sub)
                log.warning("拉取 r/%s 失败: %s", sub, e)

    if "hn" in scopes:
        stats["hn"]["attempted"] += 1
        try:
            items = hn.fetch_front_with_content(limit=15)
            min_points = int(cfg.get("hn_min_points") or 0)
            if min_points:
                items = [p for p in items if p["points"] >= min_points]
            buckets["hn:front"] = items
        except Exception as e:
            stats["hn"]["failed"] += 1
            log.warning("拉取 HN 失败: %s", e)

    if "rss" in scopes:
        for feed_cfg in cfg.get("generic_feeds") or []:
            name = feed_cfg.get("name")
            key = f"rss:{name}"
            stats["rss"]["attempted"] += 1
            try:
                items = generic_rss.fetch_feed(feed_cfg["url"])
                topic = feed_cfg.get("filter_topic")
                if topic and ai.enabled() and items:
                    try:
                        keep = ai.filter_relevant(items, topic)
                        dropped = [it["id"] for i, it in enumerate(items) if i not in keep]
                        items = [it for i, it in enumerate(items) if i in keep]
                        if dropped:
                            drops[key] = dropped
                        log.info("[%s] 相关性过滤：%d/%d 条命中主题",
                                 key, len(items), len(items) + len(dropped))
                    except Exception as e:
                        log.warning("[%s] 相关性过滤失败，按未过滤推送: %s", key, e)
                buckets[key] = items
            except Exception as e:
                stats["rss"]["failed"] += 1
                log.warning("拉取 RSS %s 失败: %s", name, e)

    if "trending" in scopes:
        gt = cfg.get("github_trending")
        if gt:
            for lang in (gt.get("languages") or [None]):
                name = lang or "all"
                stats["trending"]["attempted"] += 1
                try:
                    buckets[f"trending:{name}"] = github_trending.fetch_trending(
                        since=gt.get("since") or "daily", language=lang,
                        limit=int(gt.get("limit") or 10))
                except Exception as e:
                    stats["trending"]["failed"] += 1
                    log.warning("拉取 GitHub Trending(%s) 失败: %s", name, e)

    if "radar" in scopes:
        rc = cfg.get("hf_radar")
        if rc:
            stats["radar"]["attempted"] += 1
            try:
                buckets["radar:all"] = hf_models.fetch_radar(
                    filter_tag=rc.get("filter") or "text-generation",
                    limit=int(rc.get("limit") or 10))
            except Exception as e:
                stats["radar"]["failed"] += 1
                log.warning("拉取模型雷达失败: %s", e)

    if "papers" in scopes:
        pc = cfg.get("hf_papers")
        if pc:
            stats["papers"]["attempted"] += 1
            try:
                buckets["papers:daily"] = hf_papers.fetch_daily_papers(
                    limit=int(pc.get("limit") or 10))
            except Exception as e:
                stats["papers"]["failed"] += 1
                log.warning("拉取 HF 论文日报失败: %s", e)

    if "deals" in scopes:
        dc = cfg.get("deals") or {}
        stats["deals"]["attempted"] += 1
        try:
            ditems = deals.fetch_deals(limit=int(dc.get("fetch_limit") or 25))
            if ai.enabled() and ditems:
                try:
                    judged = ai.judge_deals(ditems)
                    keep = {d["i"] for d in judged}
                    by_idx = {d["i"]: d for d in judged}
                    dropped = [it["id"] for i, it in enumerate(ditems) if i not in keep]
                    if dropped:
                        drops["deals:radar"] = dropped
                    ditems = [dict(it, body="\n".join(
                        x for x in (_deal_note(by_idx[i]), it.get("body") or "") if x))
                        for i, it in enumerate(ditems) if i in keep]
                    log.info("[deals:radar] 羊毛判定：%d/%d 条可领",
                             len(ditems), len(ditems) + len(dropped))
                except Exception as e:
                    log.warning("[deals:radar] 羊毛判定失败，按未过滤推送: %s", e)
            buckets["deals:radar"] = ditems[:int(dc.get("limit") or 10)]
        except Exception as e:
            stats["deals"]["failed"] += 1
            log.warning("拉取羊毛雷达失败: %s", e)

    stats["drops"] = drops
    return buckets, stats


def split_new(store_data, key, items):
    """返回 (未推送过的条目列表, 是否首运行)。"""
    seen = set(store_data.get(key, []))
    fresh = [it for it in items if str(it["id"]) not in seen]
    return fresh, key not in store_data


def build_reddit_card(sub, items, fmt, sort_key, max_items):
    ordered = sorted(items, key=sort_key)[:max_items]
    body_cap, zh_cap = per_item_caps(len(ordered))

    # AI 输入与展示对齐：短文全量进 AI（译文覆盖展示的原文）；长文只进要点
    ai_items = [{"i": i,
                 "text": f"{p['title']}\n{prepare_ai_text(p.get('body'))}"}
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
        # RSS 通道拿不到分数，不把未知伪装成 0
        stat_line = ""
        if p.get("score") is not None:
            stat_line = f" · ⬆ {p['score']:,} · 💬 {p.get('comments', 0):,}"
        head = (f"**[{truncate(p['title'], 120)}]({p['url']})**"
                f"{author}{stat_line} · 🕐 {fmt(p['time'])}")
        block = head
        body_text = (p.get("body") or "").strip()
        if body_text and len(body_text) <= SHORT_BODY:
            block += f"\n{body_text}"
        zh = trans.get(i)
        if zh:
            # 长文要点模式不带"译"前缀，避免"译：要点："叠词
            block += (f"\n🌐 {truncate(zh, zh_cap)}" if zh.startswith("要点：")
                      else f"\n🌐 译：{truncate(zh, zh_cap)}")
        lines.append(block)

    if summary:
        lines.insert(0, f"**🤖 AI 总结**：{summary}")
    return build_card(
        f"🟠 Reddit · r/{sub} · {len(items)} 个新帖", "orange", lines,
        buttons=[(f"打开 r/{sub}", f"https://www.reddit.com/r/{sub}", "primary")],
    )


def build_hn_card(items, fmt, max_items):
    ordered = items[:max_items]
    body_cap, zh_cap = per_item_caps(len(ordered))

    # AI 输入：标题 + 自述正文/热评（短文全量，长文要点化）
    ai_items = [{"text": f"{p['title']}\n{prepare_ai_text(hn_content(p))}"}
                for p in ordered]

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
    for i, p in enumerate(ordered):
        by = f" · @{p['author']}" if p.get("author") else ""
        line = (f"**[{truncate(p['title'], 150)}]({p['url']})** · "
                f"▲ {p['points']:,} · 💬 {p['comments']:,}{by} · "
                f"🕐 {fmt(p['time'])} · [讨论]({p['hn_url']})")
        block = line
        content = hn_content(p)
        if content and len(content) <= SHORT_BODY:
            block += f"\n{content}"
        zh = trans.get(i)
        if zh:
            # 长文要点模式不带"译"前缀，避免"译：要点："叠词
            block += (f"\n🌐 {truncate(zh, zh_cap)}" if zh.startswith("要点：")
                      else f"\n🌐 译：{truncate(zh, zh_cap)}")
        lines.append(block)

    if summary:
        lines.insert(0, f"**🤖 AI 总结**：{summary}")
    return build_card(
        f"🟡 Hacker News · 首页精选 · {len(items)} 条", "yellow", lines,
        buttons=[("打开 Hacker News", "https://news.ycombinator.com", "primary")],
    )


def hn_content(post):
    """自述帖用正文，外链帖用热评。"""
    text = (post.get("story_text") or "").strip()
    if text:
        return text
    top = post.get("top_comment")
    if top:
        return f"热评 @{top[0]}：{top[1]}"
    return ""


def build_generic_card(name, items, fmt, sort_key, max_items, label="RSS",
                       buttons=None, color="green"):
    ordered = sorted(items, key=sort_key)[:max_items]
    body_cap, zh_cap = per_item_caps(len(ordered))

    ai_items = [{"i": i,
                 "text": f"{p['title']}\n{prepare_ai_text(p.get('body'))}"}
                for i, p in enumerate(ordered)]

    ai_result = None
    if ai.enabled() and any(x["text"].strip() for x in ai_items):
        try:
            ai_result = ai.translate_and_summarize(ai_items)
            log.info("[%s] AI 处理完成（翻译 %d 条）", name, len(ai_result["translations"]))
        except Exception as e:
            log.warning("[%s] AI 增强失败: %s", name, e)
    trans = (ai_result or {}).get("translations") or {}
    summary = (ai_result or {}).get("summary")

    lines = []
    for i, p in enumerate(ordered):
        author = f" · {p['author']}" if p["author"] else ""
        meta = fmt(p["time"])
        tail = f" · {meta}" if meta else ""
        block = f"**[{truncate(p['title'], 150)}]({p['url']})**{author}{tail}"
        body_text = (p.get("body") or "").strip()
        if body_text and len(body_text) <= SHORT_BODY:
            block += f"\n{body_text}"
        zh = trans.get(i)
        if zh:
            # 长文要点模式不带"译"前缀，避免"译：要点："叠词
            block += (f"\n🌐 {truncate(zh, zh_cap)}" if zh.startswith("要点：")
                      else f"\n🌐 译：{truncate(zh, zh_cap)}")
        lines.append(block)

    if summary:
        lines.insert(0, f"**🤖 AI 总结**：{summary}")
    name_part = f"{name} · " if name else ""
    return build_card(f"{label} · {name_part}{len(items)} 条更新", color, lines,
                      buttons=buttons)


def run(dry_run=False):
    load_env()
    with open(os.path.join(BASE, "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    push_cfg = cfg.get("push", {})
    max_items = int(push_cfg.get("max_items_per_card", 10))
    fmt, sort_key = make_times(int(cfg.get("timezone_offset_hours", 8)))

    webhook = os.environ.get("FEISHU_WEBHOOK")
    secret = os.environ.get("FEISHU_SECRET") or None
    if not dry_run and not webhook:
        raise SystemExit("缺少 FEISHU_WEBHOOK（.env 或 GitHub Secrets）")

    store_data = store.load()
    buckets, stats = collect(cfg)

    # 范围内全部源拉取失败时标记，最终以非零码退出触发告警
    dead = []
    for name in ("reddit", "hn", "rss", "trending", "radar", "papers", "deals"):
        if name not in parse_scope() or not stats[name]["attempted"]:
            continue
        s = stats[name]
        failed = len(s["failed"]) if isinstance(s["failed"], list) else s["failed"]
        if failed == s["attempted"]:
            dead.append(name)
    if dead:
        log.error("范围内全部源拉取失败: %s", "、".join(dead))

    pending = []      # (key, ids, card)
    baselines = {}    # 首运行只记基线不推送
    total_new = 0

    for key in sorted(buckets):
        fresh, first_run = split_new(store_data, key, buckets[key])
        if first_run:
            log.info("[%s] 首次运行，记录 %d 条基线，本次不推送", key, len(buckets[key]))
            baselines[key] = [str(it["id"]) for it in buckets[key]]
            continue
        if not fresh:
            log.info("[%s] 无新内容", key)
            continue
        total_new += len(fresh)
        source, name = key.split(":", 1)
        if source == "hn":
            card = build_hn_card(fresh, fmt, max_items)
        elif source in ("trending", "radar", "papers"):
            label, buttons = {
                "trending": ("🔥 GitHub Trending",
                             [("打开 GitHub Trending", "https://github.com/trending", "primary")]),
                "radar": ("🤗 模型雷达",
                          [("Hugging Face 趋势榜",
                            "https://huggingface.co/models?sort=trendingScore", "primary")]),
                "papers": ("📄 HF 论文日报",
                           [("Hugging Face Papers", "https://huggingface.co/papers", "primary")]),
            }[source]
            card = build_generic_card(name, fresh, fmt, sort_key, max_items,
                                      label=label, buttons=buttons)
        elif source == "deals":
            card = build_generic_card(
                "", fresh, fmt, sort_key, max_items,
                label="🧧 羊毛雷达", color="carmine",
                buttons=[("OpenRouter 免费模型",
                          "https://openrouter.ai/models?max_price=0", "primary")])
        elif source == "rss":
            card = build_generic_card(name, fresh, fmt, sort_key, max_items, label="📰 RSS")
        else:
            card = build_reddit_card(name, fresh, fmt, sort_key, max_items)
        if len(fresh) > max_items and source == "rss":
            card["elements"].append(
                {"tag": "note", "elements": [{"tag": "plain_text",
                                              "content": f"另有 {len(fresh) - max_items} 条将在后续卡片推送"}]}
            )
        # rss 排空模式只记录展示条目 + 被相关性过滤剔除的条目；羊毛雷达全量记录
        # 已推条目和被判定掉的线索（避免每小时反复重判）；其余源全量记录
        record_ids = [str(it["id"]) for it in fresh]
        if source == "rss":
            shown = {str(it["id"]) for it in sorted(fresh, key=sort_key)[:max_items]}
            record_ids = ([i for i in record_ids if i in shown]
                          + list(stats.get("drops", {}).get(key, [])))
        elif source == "deals":
            record_ids = record_ids + list(stats.get("drops", {}).get(key, []))
        pending.append((key, record_ids, card))

    # 发送阶段：成功才记录为已推送，失败留给下轮重试
    sent, failed = 0, 0
    for key, ids, card in pending:
        if dry_run:
            import json as _json
            print(f"\n===== DRY-RUN 将推送 [{key}] =====")
            print(_json.dumps(card, ensure_ascii=False, indent=2)[:3000])
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

    # dry-run 不落盘：只打印卡片，不消耗去重状态
    if not dry_run:
        store_data.update(baselines)
        store.save(store_data)

    log.info(
        "完成：%d 个源有新内容（%d 条），推送成功 %d 个源，失败 %d，基线初始化 %d",
        len(pending), total_new, sent, failed, len(baselines),
    )
    return 1 if (failed or dead) else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(run(dry_run="--dry-run" in sys.argv))
