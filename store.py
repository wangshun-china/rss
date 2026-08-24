"""已推送条目的本地状态（state.json），用于跨运行去重。

GitHub Actions 每次运行后会把更新后的 state.json 提交回仓库实现持久化。
"""

import json
import os

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
CAP = 500  # 每个 source 最多记录的 ID 数


def load():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save(store):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)


def record(store, key, ids):
    """合并新 ID 并截断保存。ids 应按最新在前排序。"""
    merged = list(dict.fromkeys(list(ids) + store.get(key, [])))
    store[key] = merged[:CAP]
