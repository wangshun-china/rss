"""飞书自定义机器人推送：组装卡片消息并发送到 webhook。"""

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import quote_plus

import requests

# 自定义机器人卡片上限 30KB，留余量
MAX_CARD_BYTES = 28 * 1024


def _signed_url(webhook, secret):
    if not secret:
        return webhook
    ts = str(int(time.time()))
    string_to_sign = f"{ts}\n{secret}"
    sign = base64.b64encode(
        hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    ).decode()
    sep = "&" if "?" in webhook else "?"
    return f"{webhook}{sep}timestamp={ts}&sign={quote_plus(sign)}"


def fit_card(card, max_bytes=MAX_CARD_BYTES):
    """整卡超限时逐步截断最长的文本块（每轮砍 20%），保证飞书能接受。

    不做大小预算时，满载卡片会被飞书拒绝，且失败轮次 id 不入去重状态，
    下一轮组卡更大，形成永久失败循环。
    """
    while len(json.dumps(card, ensure_ascii=False).encode("utf-8")) > max_bytes:
        divs = [e for e in card["elements"] if e.get("tag") == "div"]
        if not divs:
            break
        biggest = max(divs, key=lambda e: len(e["text"]["content"]))
        content = biggest["text"]["content"]
        if len(content) <= 150:
            card["elements"].remove(biggest)
            continue
        biggest["text"]["content"] = content[: int(len(content) * 0.8)].rstrip() + "…"
    return card


def send(webhook, secret, card):
    """发送 interactive 卡片，失败抛异常。超限卡片先自动压缩。"""
    card = fit_card(card)
    resp = requests.post(
        _signed_url(webhook, secret),
        json={"msg_type": "interactive", "card": card},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    # 旧版接口成功返回 {"code":0}，部分场景无 code 字段
    code = body.get("code", body.get("StatusCode", 0))
    if code != 0:
        raise RuntimeError(f"飞书返回错误: {body}")


def build_card(title, color, items, footer=None):
    """items: lark_md 字符串列表，条目间用分隔线。"""
    elements = []
    for i, text in enumerate(items):
        if i > 0:
            elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": text}})
    if footer:
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": footer}]})
    return {
        "header": {
            "template": color,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": elements,
    }
