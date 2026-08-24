"""飞书自定义机器人推送：组装卡片消息并发送到 webhook。"""

import base64
import hashlib
import hmac
import time
from urllib.parse import quote_plus

import requests


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


def send(webhook, secret, card):
    """发送 interactive 卡片，失败抛异常。"""
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
