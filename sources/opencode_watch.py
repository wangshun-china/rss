"""OpenCode 免费模型监视：目录（models.dev）+ 实时接口双源校验。

免费判定：models.dev 里 cost.input 与 cost.output 均为 0。
有效性校验：模型还必须在 OpenCode 实时在售列表中——目录会滞后，
只看目录会漏报（2026-09 space-bunny-free 从实时接口下架、目录仍标免费，
就是靠实时接口这一路才发现的）。

本模块只负责拉取快照；与上次快照对比、产生下线事件由 main.py 的
zenwatch scope 完成。
"""

import requests

UA = {"User-Agent": "Mozilla/5.0"}
_API = "https://models.dev/api.json"

# provider -> 实时在售模型列表（OpenAI 兼容 /models，无需密钥）
_LIVE_API = {
    "opencode-go": "https://opencode.ai/zen/go/v1/models",
    "opencode": "https://opencode.ai/zen/v1/models",
}
_MIN_LIVE = 3  # 实时列表少于该条数视为接口异常，不参与判定


def fetch_free_models(providers=("opencode-go",)):
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


def fetch_live_models(providers=("opencode-go",)):
    """实时接口的在售模型 id 集合；接口异常时该 provider 记 None（判定跳过）。"""
    out = {}
    for prov in providers:
        url = _LIVE_API.get(prov)
        if not url:
            out[prov] = None
            continue
        try:
            r = requests.get(url, headers=UA, timeout=30)
            r.raise_for_status()
            ids = {m.get("id") for m in (r.json().get("data") or []) if m.get("id")}
            out[prov] = ids if len(ids) >= _MIN_LIVE else None
        except Exception:
            out[prov] = None
    return out

