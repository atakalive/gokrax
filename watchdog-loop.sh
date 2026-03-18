#!/bin/bash
# watchdog-loop.sh — 20秒間隔でwatchdog.pyを実行するデーモン
# crontabの1分間隔cronの代替。flock排他で二重起動防止。
#
# 起動: nohup bash watchdog-loop.sh &
# 停止: kill $(cat /tmp/gokrax-watchdog-loop.pid)

LOCKFILE="/tmp/gokrax-watchdog-loop.lock"
PIDFILE="/tmp/gokrax-watchdog-loop.pid"
INTERVAL=20
DIR="$(cd "$(dirname "$0")" && pwd)"

exec 200>"$LOCKFILE"
flock -n 200 || { echo "Already running"; exit 1; }

echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"; exit 0' SIGTERM SIGINT

while true; do
    cd "$DIR"
    flock -n /tmp/gokrax-watchdog.lock python3 watchdog.py >> /tmp/gokrax-watchdog.log 2>&1
    sleep "$INTERVAL"
done
