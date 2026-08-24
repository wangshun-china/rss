#!/bin/sh
# 容器内调度器：启动即执行一次，之后每个整点 17 分（容器时区）执行下一轮。
set -u

echo "[scheduler] started at $(date '+%F %T %Z')"

while true; do
  if python /app/main.py; then
    echo "[scheduler] run finished ok"
  else
    echo "[scheduler] run failed, will retry next cycle"
  fi
  delay=$(python - <<'PYEOF'
import datetime

now = datetime.datetime.now()
target = now.replace(minute=17, second=0, microsecond=0)
if target <= now:
    target += datetime.timedelta(hours=1)
print(int((target - now).total_seconds()))
PYEOF
)
  echo "[scheduler] sleeping ${delay}s until next :17"
  sleep "$delay"
done
