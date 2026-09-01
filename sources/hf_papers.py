"""Hugging Face Daily Papers 论文日报：官方 JSON API 免凭据，按赞数排序。

摘要（英文，普遍上千字）交给卡片构建器的长文要点逻辑提炼，不在卡里铺全文。
"""

import requests

_API = "https://huggingface.co/api/daily_papers"


def fetch_daily_papers(limit=10):
    r = requests.get(_API, timeout=30)
    r.raise_for_status()
    papers = sorted(
        r.json(),
        key=lambda p: int((p.get("paper") or {}).get("upvotes")
                          or p.get("upvotes") or 0),
        reverse=True,
    )
    items = []
    for p in papers:
        paper = p.get("paper") or {}
        pid = paper.get("id") or p.get("id")
        title = (paper.get("title") or p.get("title") or "").strip()
        if not pid or not title:
            continue
        upvotes = int(paper.get("upvotes") or p.get("upvotes") or 0)
        items.append({
            "id": f"hfpaper-{pid}",
            "title": title,
            "url": f"https://huggingface.co/papers/{pid}",
            "author": f"▲ {upvotes:,} 赞",  # 借作者位展示热度，长文摘要被要点替换也不丢
            "time": (p.get("publishedAt") or "")[:10] or None,
            "body": (paper.get("summary") or p.get("summary") or "").strip(),
        })
        if len(items) >= limit:
            break
    return items
