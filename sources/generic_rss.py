"""通用 RSS/Atom 订阅源：config.yaml 的 generic_feeds（name + url），无凭据。

适用于官方博客、GitHub Releases (.atom)、arXiv、V2EX、YouTube 频道、
Google News 关键词等一切标准 feed。正文取 content/summary 并转纯文本。
"""

import requests
import feedparser

from sources.text import html_to_text

UA = "rss-feishu-bot/0.1 (personal feed aggregator)"


def fetch_feed(url, limit=30):
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    if getattr(feed, "bozo", False) and not feed.entries:
        raise RuntimeError(f"RSS 解析失败 {url}: {feed.bozo_exception}")
    items = []
    for e in feed.entries[:limit]:
        content = e.get("content") or []
        body_html = (content[0].get("value") if content else None) \
            or e.get("summary") or e.get("description") or ""
        items.append({
            "id": e.get("id") or e.get("link"),
            "title": e.get("title") or "(无标题)",
            "url": e.get("link") or "",
            "author": e.get("author") or "",
            "time": e.get("published") or e.get("updated"),
            "body": html_to_text(body_html),
        })
    return items
