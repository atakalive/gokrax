#!/bin/bash
# watchdog-loop.sh — 10秒間隔でwatchdog.pyを実行するデーモン
# crontabの1分間隔cronの代替。flock排他で二重起動防止。
#
# 起動: nohup bash watchdog-loop.sh &
# 停止: kill $(cat /tmp/gokrax-watchdog-loop.pid)

set -m   # job control を有効化（前景子を独立 PGID にして group-kill 可能にする）

LOCKFILE="/tmp/gokrax-watchdog-loop.lock"
PIDFILE="/tmp/gokrax-watchdog-loop.pid"
PGIDFILE="/tmp/gokrax-watchdog-loop-child.pgid"
INTERVAL=10
DIR="$(cd "$(dirname "$0")" && pwd)"

# cron flock ラッパーから継承された fd を閉じる（二重ロック防止 #145）
for _fd in 3 4 5 6 7 8 9; do eval "exec ${_fd}>&-" 2>/dev/null; done

# Stale pidfile cleanup（プロセス死亡後の残留pidfile除去 #145）
if [ -f "$PIDFILE" ]; then
    _old_pid=$(cat "$PIDFILE" 2>/dev/null)
    if [ -n "$_old_pid" ] && ! kill -0 "$_old_pid" 2>/dev/null; then
        rm -f "$PIDFILE"
        rm -f "$PGIDFILE"
    fi
fi

exec 200>"$LOCKFILE"
flock -n 200 || { echo "Already running"; exit 1; }

CHILD_PGID=""
SLEEP_PID=""

_shutdown() {
    if [ -n "$SLEEP_PID" ]; then
        kill -s TERM "$SLEEP_PID" 2>/dev/null || true
    fi
    if [ -n "$CHILD_PGID" ]; then
        kill -s TERM -- -"$CHILD_PGID" 2>/dev/null || true
        wait "$CHILD_PGID" 2>/dev/null
        kill -s KILL -- -"$CHILD_PGID" 2>/dev/null || true
    fi
    exit 0
}

trap '_shutdown' SIGTERM SIGINT
trap 'rm -f "$PIDFILE" "$PGIDFILE"' EXIT

echo $$ > "$PIDFILE"

while true; do
    cd "$DIR"
    flock -n /tmp/gokrax-watchdog.lock bash -c 'exec 200>&-; exec python3 watchdog.py' >> /tmp/gokrax-watchdog.log 2>&1 &
    CHILD_PGID=$!
    echo "$CHILD_PGID" > "$PGIDFILE"
    wait "$CHILD_PGID"
    CHILD_PGID=""
    : > "$PGIDFILE"
    sleep "$INTERVAL" &
    SLEEP_PID=$!
    wait "$SLEEP_PID"
    SLEEP_PID=""
done
