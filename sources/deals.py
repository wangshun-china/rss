"""羊毛雷达：汇聚"可领取的免费 LLM 额度"信号源（均免凭据）。

- OpenRouter 免费 prompt/completion 模型（结构化，最可靠，取最新上架）
- Reddit 关键词搜索 RSS（SillyTavernAI / LocalLLaMA，7 天窗口）
- GitHub 羊毛清单仓库的 commits.atom（清单更新 = 发现新免费源）
- HN Algolia 关键词搜索（7 天窗口，仅 10 赞以上）

linux.do（Cloudflare 防护 403）与 NodeSeek（无 RSS）暂未接入。
所有条目无差别混在一起，由 main.py 经 AI 判定"是否真能领"后才推送；
被过滤的条目记入去重状态，不会每小时反复重判。
"""

import re
import time

import requests
import feedparser

UA_WEB = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
UA_REDDIT = "rss-feishu-bot/0.1 (personal feed aggregator)"

REDDIT_QUERIES = [
    ("SillyTavernAI", "free credits OR free token OR free tier"),
    ("LocalLLaMA", "free credits OR promo OR giveaway OR free tier"),
]
GITHUB_LISTS = [
    "mnfst/awesome-free-llm-apis",
    "open-free-llm-api/awesome-freellm-apis",
]

_TITLE_KEY = re.compile(r"[^a-z0-9\u4e00-\u9fff]+")


def _norm_title(title):
    return _TITLE_KEY.sub("", (title or "").lower())[:60]


def _openrouter():
    r = requests.get("https://openrouter.ai/api/v1/models", timeout=30)
    r.raise_for_status()
    free = [m for m in r.json().get("data", [])
            if (m.get("pricing") or {}).get("prompt") == "0"
            and (m.get("pricing") or {}).get("completion") == "0"]
    items = []
    for m in sorted(free, key=lambda x: x.get("created") or 0, reverse=True)[:5]:
        mid = m.get("id") or ""
        if not mid:
            continue
        ctx = int(m.get("context_length") or 0)
        created = m.get("created")
        items.append({
            "id": f"openrouter-{mid}",
            "title": m.get("name") or mid,
            "url": f"https://openrouter.ai/{mid}",
            "author": "OpenRouter",
            "time": None,
            "body": (f"🆓 免费模型上新 · 上下文 {ctx:,} tokens"
                     + (f" · 上架 {time.strftime('%m-%d', time.gmtime(created))}" if created else "")),
        })
    return items


def _reddit():
    items = []
    for sub, query in REDDIT_QUERIES:
        try:
            r = requests.get(f"https://www.reddit.com/r/{sub}/search.rss",
                             params={"q": query, "restrict_sr": "on",
                                     "sort": "new", "t": "week"},
                             headers={"User-Agent": UA_REDDIT}, timeout=30)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            for e in feedparser.parse(r.content).entries[:8]:
                items.append({
                    "id": e.get("id") or e.get("link"),
                    "title": e.get("title") or "(无标题)",
                    "url": e.get("link") or f"https://www.reddit.com/r/{sub}",
                    "author": f"r/{sub}",
                    "time": e.get("published"),
                    "body": e.get("summary") or "",
                })
        except Exception:
            pass  # 单渠道失败不拖垮整个雷达
        time.sleep(2)  # Reddit 匿名通道限流敏感，搜索请求间隔拉大
    return items


def _github_lists():
    items = []
    for repo in GITHUB_LISTS:
        try:
            r = requests.get(f"https://github.com/{repo}/commits.atom",
                             headers=UA_WEB, timeout=30)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            for e in feedparser.parse(r.content).entries[:5]:
                title = e.get("title", "").replace("\n", " ").strip()
                # 跳过纯机器人同步提交
                if re.search(r"sync README|regenerate README|skip ci", title, re.I):
                    continue
                items.append({
                    "id": e.get("id") or e.get("link"),
                    "title": f"[清单更新] {title}",
                    "url": e.get("link") or f"https://github.com/{repo}",
                    "author": repo.split("/")[0],
                    "time": e.get("updated"),
                    "body": "",
                })
        except Exception:
            pass
    return items


def _hn():
    try:
        r = requests.get("https://hn.algolia.com/api/v1/search", params={
            "query": '"free credits" OR "free tier" API',
            "tags": "story",
            "numericFilters": f"created_at_i>{int(time.time()) - 7 * 86400},points>=10",
        }, timeout=30)
        r.raise_for_status()
    except Exception:
        return []
    items = []
    for h in r.json().get("hits", [])[:5]:
        hid = h.get("objectID")
        title = h.get("title") or "(untitled)"
        url = h.get("url") or f"https://news.ycombinator.com/item?id={hid}"
        items.append({
            "id": f"hn-{hid}",
            "title": title,
            "url": url,
            "author": "Hacker News",
            "time": h.get("created_at"),
            "body": f"▲ {int(h.get('points') or 0):,} · "
                    f"https://news.ycombinator.com/item?id={hid}",
        })
    return items


def fetch_deals(limit=25):
    """汇聚各渠道羊毛信号，按标题近似去重后返回。"""
    raw = []
    for fetcher in (_openrouter, _reddit, _github_lists, _hn):
        try:
            raw.extend(fetcher())
        except Exception:
            pass

    seen, deduped = set(), []
    for it in raw:
        key = _norm_title(it["title"])
        if key and key in seen:
            continue
        seen.add(key)
        deduped.append(it)
    return deduped[:limit]
