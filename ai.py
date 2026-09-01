"""AI 翻译与总结：调用 OpenAI 兼容接口处理推文批次。

环境变量：
    AI_API_BASE  接口基础地址，如 https://xxx/v1（未设置则禁用 AI 增强）
    AI_API_KEY   Bearer Key
    AI_MODEL     模型名，默认 deepseek-v4-flash-0731
"""

import json
import os
import time

import requests

BASE = os.environ.get("AI_API_BASE", "").rstrip("/")
KEY = os.environ.get("AI_API_KEY", "")
MODEL = os.environ.get("AI_MODEL") or "deepseek-v4-flash-0731"

SYSTEM_PROMPT = """你是信息流内容助手。用户给出若干条编号内容（可能是帖子、文章、仓库榜单等），请只输出一个 JSON 对象（禁止 markdown 代码块围栏）：
{"summary": "中文总结", "translations": [{"i": 编号, "zh": "中文翻译"}]}
规则：
1. summary 用不超过 100 字的中文概括整批内容讲了什么、共同主题或趋势是什么，突出关键信息，不要逐条罗列。
2. 只翻译非中文内容；纯中文内容不要出现在 translations 里。
3. 翻译要自然流畅，保留专有名词、数字和链接，长度不超过原文两倍。"""


def enabled():
    return bool(BASE and KEY)


FILTER_PROMPT = """你是信息流过滤器。判断每条编号内容是否与给定主题相关，只输出一个 JSON 对象（禁止 markdown 代码块围栏）：
{"relevant": [相关内容的编号, ...]}
标准从严：拿不准的一律视为不相关。编号按原样输出，不要增删。"""


def filter_relevant(items, topic, timeout=60):
    """逐条判断是否与 topic 相关，返回相关下标集合。失败时放行全部（宁可多推不可漏推）。"""
    numbered = "\n".join(f"[{i}] {(t['text'] or '').strip()[:400]}"
                         for i, t in enumerate(items))
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": FILTER_PROMPT},
            {"role": "user", "content": f"主题：{topic}\n\n内容：\n{numbered}"},
        ],
        "temperature": 0,
    }
    headers = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    resp = None
    last_err = ""
    for attempt in range(3):
        try:
            resp = requests.post(f"{BASE}/chat/completions",
                                 headers=headers, json=payload, timeout=timeout)
        except requests.RequestException as e:
            last_err = str(e)
            time.sleep(6 * (attempt + 1))
            continue
        if resp.status_code not in (429, 500, 502, 503, 504):
            break
        last_err = f"HTTP {resp.status_code}"
        time.sleep((10, 25)[attempt] if attempt < 2 else 25)
    if resp is None or resp.status_code != 200:
        raise RuntimeError(f"AI 过滤接口失败: {last_err}")

    data = _parse_json(resp.json()["choices"][0]["message"]["content"])
    keep = set()
    for idx in data.get("relevant") or []:
        try:
            i = int(idx)
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(items):
            keep.add(i)
    return keep


def _parse_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"AI 未返回 JSON: {text[:120]}")
    return json.loads(text[start:end + 1])


def translate_and_summarize(items, timeout=90):
    """items 为按展示顺序排列的内容列表，返回 {"summary": str|None, "translations": {idx: str}}。"""
    numbered = "\n".join(f"[{i}] {(t['text'] or '').strip()[:1500]}"
                         for i, t in enumerate(items))
    headers = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

    def _call(items):
        payload = {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": items},
            ],
            "temperature": 0.2,
        }
        resp = None
        last_err = ""
        for attempt in range(3):
            try:
                resp = requests.post(f"{BASE}/chat/completions",
                                     headers=headers, json=payload, timeout=timeout)
            except requests.RequestException as e:
                last_err = str(e)
                time.sleep(6 * (attempt + 1))
                continue
            if resp.status_code == 400 and attempt == 0:
                # 个别推文内容触发上游 400 时，减半输入再试一次
                payload["messages"][1]["content"] = numbered[:max(200, len(numbered) // 2)]
                continue
            if resp.status_code not in (429, 500, 502, 503, 504):
                return resp
            last_err = f"HTTP {resp.status_code}"
            time.sleep((10, 25)[attempt] if attempt < 2 else 25)
        raise RuntimeError(f"AI 接口失败: {last_err}")

    resp = _call(numbered)
    body = resp.json()
    if body.get("error"):
        raise RuntimeError(f"AI 接口错误: {body['error']}")
    content = body["choices"][0]["message"]["content"]
    data = _parse_json(content)

    translations = {}
    for item in data.get("translations") or []:
        try:
            idx = int(item.get("i"))
            zh = (item.get("zh") or "").strip()
            if zh:
                translations[idx] = zh[:8000]
        except (TypeError, ValueError):
            continue
    return {"summary": (data.get("summary") or "").strip() or None,
            "translations": translations}
