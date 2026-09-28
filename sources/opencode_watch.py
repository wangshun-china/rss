"""OpenCode 免费模型监视：models.dev 官方目录（sst/opencode 同家维护）。

免费判定全自动：cost.input 与 cost.output 均为 0 即免费。
本模块只负责拉取"当前免费名单快照"；与上次快照对比、只在模型下线时
产生事件，由 main.py 的 zenwatch scope 完成。
"""

import requests

UA = {"User-Agent": "Mozilla/5.0"}
_API = "https://models.dev/api.json"


def fetch_free_models(providers=("opencode-go", "opencode")):
    """返回 {provider: {"label": 显示名, "doc": 文档链接, "free": [模型 id]}}。"""
    r = requests.get(_API, headers=UA, timeout=30)
    r.raise_for_status()
    data = r.json()

    snapshot = {}
    for prov in providers:
        p = data.get(prov)
        if not p:
            continue
        free = []
        for mid, m in (p.get("models") or {}).items():
            cost = m.get("cost") or {}
            if cost.get("input") == 0 and cost.get("output") == 0:
                free.append(mid)
        snapshot[prov] = {
            "label": p.get("name") or prov,
            "doc": p.get("doc") or f"https://opencode.ai/docs/{prov.removeprefix('opencode-')}",
            "free": sorted(free),
        }
    if not snapshot:
        raise RuntimeError(f"models.dev 中找不到目标 provider: {providers}")
    return snapshot
