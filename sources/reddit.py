"""拉取 subreddit 最新帖子：优先匿名 RSS，被 403/429 时回退 OAuth API。

国内服务器访问 Reddit 被整站阻断时，可设置 REDDIT_PROXY（如 http://host.docker.internal:7890）
让本模块的请求走代理。
"""

import os
import time

import requests
import feedparser

UA = "rss-feishu-bot/0.1 (personal feed aggregator)"
_proxy = os.environ.get("REDDIT_PROXY") or None
PROXIES = {"http": _proxy, "https": _proxy} if _proxy else None


def _get_rss(url):
    """匿名 RSS；数据中心 IP 频繁被 429 限流，用较长退避多次重试。"""
    resp = None
    for attempt in range(4):
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=30, proxies=PROXIES)
        if resp.status_code != 429:
            return resp
        time.sleep((10, 25, 50)[attempt])
    return resp


def _oauth_token(client_id, client_secret):
    resp = requests.post(
        "https://www.reddit.com/api/v1/access_token",
        auth=(client_id, client_secret),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": UA},
        timeout=30,
        proxies=PROXIES,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _from_oauth(sub, limit, client_id, client_secret):
    token = _oauth_token(client_id, client_secret)
    resp = requests.get(
        f"https://oauth.reddit.com/r/{sub}/new",
        params={"limit": limit},
        headers={"Authorization": f"bearer {token}", "User-Agent": UA},
        timeout=30,
        proxies=PROXIES,
    )
    resp.raise_for_status()
    posts = []
    for child in resp.json()["data"]["children"]:
        d = child["data"]
        posts.append(
            {
                "id": d["id"],
                "title": d["title"],
                "url": "https://www.reddit.com" + d["permalink"],
                "author": d.get("author") or "",
                "time": d.get("created_utc"),
                "comments": int(d.get("num_comments") or 0),
            }
        )
    return posts


def fetch_post_detail(url):
    """拉取单帖详情（正文/分数/评论数）。被拦截或无正文时返回 None。"""
    try:
        r = requests.get(url.rstrip("/") + ".json",
                         headers={"User-Agent": UA},
                         timeout=30, proxies=PROXIES)
        if r.status_code != 200:
            return None
        post = r.json()[0]["data"]["children"][0]["data"]
        return {
            "selftext": (post.get("selftext") or "").strip(),
            "score": int(post.get("score") or 0),
            "num_comments": int(post.get("num_comments") or 0),
        }
    except Exception:
        return None


def fetch_subreddit(sub, limit=25, client_id=None, client_secret=None):
    """返回该版块最新帖子列表（最新在前）。"""
    try:
        resp = _get_rss(f"https://www.reddit.com/r/{sub}/new/.rss?limit={limit}")
        if resp.status_code in (403, 429):
            if not (client_id and client_secret):
                raise RuntimeError(
                    f"r/{sub} RSS 返回 {resp.status_code}（数据中心 IP 常被 Reddit 拦截）。"
                    "请在 .env / GitHub Secrets 配置 REDDIT_CLIENT_ID/SECRET 以走 OAuth。"
                )
            return _from_oauth(sub, limit, client_id, client_secret)
        resp.raise_for_status()
    except requests.RequestException:
        if client_id and client_secret:
            return _from_oauth(sub, limit, client_id, client_secret)
        raise

    feed = feedparser.parse(resp.content)
    if getattr(feed, "bozo", False) and not feed.entries:
        raise RuntimeError(f"r/{sub} RSS 解析失败: {feed.bozo_exception}")
    posts = []
    for e in feed.entries[:limit]:
        posts.append(
            {
                # entry.id 形如 t3_1vwqd8k，与 OAuth 返回的纯 id 对齐
                "id": e.get("id", "").removeprefix("t3_") or e.get("link", ""),
                "title": e.get("title") or "(无标题)",
                "url": e.get("link") or f"https://www.reddit.com/r/{sub}",
                "author": (e.get("author") or "").removeprefix("/u/"),
                "time": e.get("published"),
                "comments": 0,
            }
        )
    return posts
