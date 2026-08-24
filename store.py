"""已推送条目的本地状态（state.json），用于跨运行去重。

容器部署时通过 RSS_DATA_DIR 指向挂载卷，保证状态在容器重建后保留。
"""

import json
import os

_DATA_DIR = os.environ.get("RSS_DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(_DATA_DIR, "state.json")
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
