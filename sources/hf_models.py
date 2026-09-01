"""Hugging Face 模型雷达：公开 API 免凭据，盯趋势榜上的新面孔。

id 带日期戳：同一模型连续多日上榜会如实重复推送（与 GitHub Trending 同策略）。
limit 控制在单卡容量内（默认 10），趋势类源每天只跑一次、一次推完。
"""

import requests

_API = "https://huggingface.co/api/models"

_SHOW_TAGS = ("gguf", "awq", "quantization", "conversational", "text-generation",
              "vision", "reasoning", "code", "embedding")


def _notable_tags(tags):
    out = []
    for t in tags or []:
        low = t.lower()
        if any(k in low for k in _SHOW_TAGS):
            out.append(t)
        if len(out) >= 3:
            break
    return out


def fetch_radar(filter_tag="text-generation", limit=10):
    r = requests.get(_API, params={
        "sort": "trendingScore", "direction": -1,
        "limit": limit, "filter": filter_tag,
    }, timeout=30)
    r.raise_for_status()

    from datetime import datetime, timedelta, timezone
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

    items = []
    for m in r.json():
        mid = (m.get("modelId") or "").strip()
        if not mid:
            continue
        parts = [f"⬇ {int(m.get('downloads') or 0):,} · ♥ {int(m.get('likes') or 0):,}"]
        created = (m.get("createdAt") or "")[:10]
        if created:
            parts.append(f"📅 {created}")
        tags = _notable_tags(m.get("tags"))
        if tags:
            parts.append(" · ".join(tags))
        items.append({
            "id": f"{mid.lower()}@{stamp}",
            "title": mid,
            "url": f"https://huggingface.co/{mid}",
            "author": mid.split("/")[0] if "/" in mid else "",
            "time": None,  # 保持 API 返回的趋势排序
            "body": "\n".join(parts),
        })
    return items
