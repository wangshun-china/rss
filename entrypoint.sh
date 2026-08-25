#!/bin/sh
# 常驻进程：每小时 advanced_search + since_id 增量拉取 X 新推文并推送飞书
set -e
echo "[poller] starting at $(date '+%F %T %Z')"
exec python /app/poller.py
