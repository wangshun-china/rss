"""Hacker News 首页热帖（Algolia 官方 API，免费无需认证）。

自述帖（Ask HN 等）带 story_text；外链帖没有正文，取一条热评作为内容补充
（Algolia search 的相关度排序接近 HN 评论排序，评论无公开分数可用）。
"""

import time

import requests

from sources.text import html_to_text


def fetch_front(limit=15):
    r = requests.get("https://hn.algolia.com/api/v1/search",
                     params={"tags": "front_page", "hitsPerPage": limit},
                     timeout=30)
    r.raise_for_status()
    items = []
    for h in r.json().get("hits", []):
        hid = h["objectID"]
        items.append({
            "id": hid,
            "title": h.get("title") or "(untitled)",
            "url": h.get("url") or f"https://news.ycombinator.com/item?id={hid}",
            "hn_url": f"https://news.ycombinator.com/item?id={hid}",
            "points": int(h.get("points") or 0),
            "comments": int(h.get("num_comments") or 0),
            "author": h.get("author") or "",
            "time": h.get("created_at"),
            "story_text": html_to_text(h.get("story_text") or ""),
        })
    return items


def _readable(text, min_len=40):
    """去掉引用行后仍足够长才算可读热评，避免拿 '>' 开头的纯引用凑数。"""
    body = "\n".join(line for line in text.splitlines()
                     if not line.lstrip().startswith(">")).strip()
    return body if len(body) >= min_len else ""


def fetch_top_comment(story_id):
    """返回 (作者, 纯文本) 热评；无评论或接口异常时返回 None。"""
    try:
        r = requests.get(
            "https://hn.algolia.com/api/v1/search",
            params={"tags": f"comment,story_{story_id}", "hitsPerPage": 5},
            timeout=30)
        r.raise_for_status()
        for c in r.json().get("hits", []):
            text = html_to_text(c.get("comment_text") or "")
            if _readable(text):
                return c.get("author") or "", text
    except Exception:
        return None
    return None


def fetch_front_with_content(limit=15):
    """首页帖子 + 外链帖的热评补充（自述帖已有 story_text，不再多查一次）。"""
    items = fetch_front(limit)
    for p in items:
        if not p["story_text"]:
            p["top_comment"] = fetch_top_comment(p["id"])
            time.sleep(0.3)  # 轻微间隔，礼貌对待公共接口
    return items
