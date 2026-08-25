"""Hacker News 首页热帖（Algolia 官方 API，免费无需认证）。"""

import requests


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
        })
    return items
