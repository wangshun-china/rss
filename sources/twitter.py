"""通过 twitterapi.io 拉取用户最新推文。"""

import time

import requests

API = "https://api.twitterapi.io/twitter/user/last_tweets"


def _get(api_key, params):
    """带 429 退避重试的 GET。"""
    resp = None
    for attempt in range(3):
        resp = requests.get(
            API,
            headers={"X-API-Key": api_key},
            params=params,
            timeout=30,
        )
        if resp.status_code != 429:
            break
        time.sleep(3 * (attempt + 1))
    return resp


def fetch_user_tweets(api_key, username, limit=20):
    """返回该用户时间线（最新在前），含原创/转推/回复。"""
    resp = _get(api_key, {"userName": username, "pageSize": min(limit, 100)})
    resp.raise_for_status()
    body = resp.json()
    if body.get("status") != "success":
        raise RuntimeError(f"@{username} 接口返回异常: {body.get('msg')}")
    data = body.get("data") or {}

    tweets = []
    for t in (data.get("tweets") or []):
        tweets.append(
            {
                "id": t["id"],
                "text": t.get("text") or "",
                "url": t.get("url") or f"https://x.com/{username}/status/{t['id']}",
                "time": t.get("createdAt"),
                # type: tweet / retweet / quote 等
                "type": t.get("type") or "tweet",
                "is_reply": bool(t.get("isReply")),
                "reply_to": t.get("inReplyToUsername") or "",
                "likes": int(t.get("favoriteCount") or 0),
            }
        )
    return tweets[:limit]
