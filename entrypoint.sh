#!/bin/sh
# 常驻进程：WebSocket 实时接收 X 推文并推送飞书（断线自动重连）
set -e
echo "[stream] starting at $(date '+%F %T %Z')"
exec python /app/stream_x.py
