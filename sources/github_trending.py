"""GitHub Trending 订阅：无官方 RSS/API，解析 trending 页面（服务端渲染，结构稳定）。

按仓库 id 去重——同一仓库只在第一次上榜时推送；配上排空语义（main.py 的
trending 源与 rss 同策略），单日上榜的仓库会在后续小时逐步推完。
"""

import re
from datetime import datetime, timezone
from html import unescape

import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"

_REPO_RE = re.compile(r'<h2[^>]*>.*?<a[^>]+href="/([^"?]+)"', re.S)
_DESC_RE = re.compile(r'<p[^>]*class="[^"]*col-9[^"]*"[^>]*>(.*?)</p>', re.S)
_STARS_RE = re.compile(r'href="/[^"]+/stargazers"[^>]*>.*?</svg>\s*([\d,]+)\s*</a>', re.S)
_TODAY_RE = re.compile(r"([\d,]+)\s+stars\s+(today|this week|this month)")
_LANG_RE = re.compile(r'itemprop="programmingLanguage">([^<]+)<')

_PERIOD_ZH = {"today": "今日", "this week": "本周", "this month": "本月"}


def _clean(text):
    return re.sub(r"\s+", " ", unescape(text or "")).strip()


def fetch_trending(since="daily", language=None, limit=25):
    """返回该榜单条目（按页面排名排序）。since: daily/weekly/monthly。"""
    params = {"since": since}
    if language:
        params["language"] = language
    r = requests.get("https://github.com/trending",
                     headers={"User-Agent": UA}, params=params, timeout=30)
    r.raise_for_status()

    period = _TODAY_RE.search(r.text)
    period_word = period.group(2) if period else "today"
    period_zh = _PERIOD_ZH.get(period_word, "今日")

    items = []
    for chunk in r.text.split('<article class="Box-row">')[1:]:
        repo = _REPO_RE.search(chunk)
        if not repo:
            continue
        path = repo.group(1).strip("/")
        stars = _STARS_RE.search(chunk)
        today = _TODAY_RE.search(chunk)
        desc = _DESC_RE.search(chunk)
        lang = _LANG_RE.search(chunk)
        body_parts = []
        if stars:
            line = f"⭐ {stars.group(1)}"
            if today:
                line += f"（{period_zh} +{today.group(1)}）"
            body_parts.append(line)
        if lang:
            body_parts.append(lang.group(1))
        text = _clean(desc.group(1)) if desc else ""
        if text:
            body_parts.append(text)
        items.append({
            "id": path.lower(),
            "title": path,
            "url": f"https://github.com/{path}",
            "author": "",
            "time": None,
            "body": "\n".join(body_parts),
        })
        if len(items) >= limit:
            break
    return items
