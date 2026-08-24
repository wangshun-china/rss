"""通过 Twitter syndication 嵌入接口拉取用户时间线（无需任何认证）。

这是官方给网页嵌入组件用的公开接口，返回的 HTML 内嵌 __NEXT_DATA__ JSON，
含最近约百条推文。国内服务器需经代理访问（走 TWITTER_PROXY 环境变量）。

注意：该接口的条目顺序不保证纯倒序（会掺入置顶/热门内容），
因此解析后按发布时间重排并过滤超龄推文（TWITTER_MAX_AGE_HOURS，默认 72h），
避免通道切换时把历史推文当成新内容推送。
"""

import html as htmllib
import json
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
BASE = "https://syndication.twitter.com/srv/timeline-profile/screen-name/{user}"
MAX_AGE_HOURS = float(os.environ.get("TWITTER_MAX_AGE_HOURS") or 72)

_proxy = os.environ.get("TWITTER_PROXY") or None
PROXIES = {"http": _proxy, "https": _proxy} if _proxy else None


def _fetch_html(username):
    """带重试地抓取时间线页面 HTML；syndication 对 IP 有短窗口限流，需长退避。"""
    url = BASE.format(user=username)
    resp = None
    last_err = ""
    for attempt in range(4):
        try:
            resp = requests.get(url, headers={"User-Agent": UA},
                                timeout=30, proxies=PROXIES)
            if resp.status_code == 200:
                return resp.text
            last_err = f"HTTP {resp.status_code}"
        except requests.RequestException as e:
            last_err = str(e)
        if attempt < 3:
            time.sleep((10, 25, 45)[attempt])
    raise RuntimeError(f"syndication 拉取失败: {last_err}")


def _expand_links(text, entities):
    """把正文里的 t.co 短链还原为真实链接（含图片/视频占位链）。"""
    if not isinstance(entities, dict):
        return text
    for group in ("urls", "media"):
        for u in entities.get(group) or []:
            short, exp = u.get("url"), u.get("expanded_url")
            if short and exp:
                text = text.replace(short, exp)
    return text


def fetch_user_tweets(username, limit=20):
    """返回该用户最新推文（最新在前），兼容原 twitterapi.io 的字段结构。"""
    page = _fetch_html(username)
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>',
                  page, re.S)
    if not m:
        raise RuntimeError(f"@{username} 页面无 __NEXT_DATA__（可能被风控）")
    data = json.loads(m.group(1))
    entries = data["props"]["pageProps"]["timeline"]["entries"]

    now = datetime.now(timezone.utc)
    parsed = []
    for e in entries:
        if e.get("type") != "tweet":
            continue
        t = e["content"].get("tweet") or {}
        tid = t.get("id_str")
        text = htmllib.unescape(t.get("full_text") or "").strip()
        if not tid or not text:
            continue
        is_retweet = text.startswith("RT @") or "retweeted_status_result" in t
        text = _expand_links(text, t.get("entities"))
        try:
            dt = parsedate_to_datetime(t.get("created_at") or "")
        except (TypeError, ValueError):
            dt = None
        parsed.append((dt, {
            "id": tid,
            "text": text,
            "url": t.get("permalink") or f"https://x.com/{username}/status/{tid}",
            "time": t.get("created_at"),
            "type": "retweet" if is_retweet else "tweet",
            "is_reply": bool(t.get("in_reply_to_status_id_str")),
            "reply_to": t.get("in_reply_to_screen_name") or "",
            "likes": int(t.get("favorite_count") or 0),
        }))

    # 按发布时间倒序取最新，并丢弃超龄条目（置顶/热门等历史内容）
    parsed.sort(key=lambda x: x[0].timestamp() if x[0] else 0, reverse=True)
    cutoff = now.timestamp() - MAX_AGE_HOURS * 3600
    return [t for dt, t in parsed[:limit * 2]
            if dt is None or dt.timestamp() >= cutoff][:limit]
