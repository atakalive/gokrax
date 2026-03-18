#!/bin/bash
# check-reviewer-health.sh — レビュアーのヘルスチェック
# journalctlからrate limitエラーとlane wait exceededを検出し、
# 直近N分間でエラーが多いエージェントを報告する。
#
# Usage: bash check-reviewer-health.sh [minutes=10]

MINUTES="${1:-10}"
REVIEWERS="pascal leibniz hanfei dijkstra kaneko"

echo "=== Reviewer Health Check (past ${MINUTES}m) ==="
echo ""

# 1. lane wait exceeded（エージェント名付き）
echo "--- Lane Wait Exceeded ---"
for agent in $REVIEWERS; do
    count=$(journalctl --user -u openclaw-gateway --since "${MINUTES} min ago" --no-pager 2>/dev/null \
        | grep -c "lane=session:agent:${agent}:main")
    if [ "$count" -gt 0 ]; then
        echo "⚠️  $agent: $count lane wait(s)"
    fi
done
echo ""

# 2. rate limit errors（エージェント名なし、総数のみ）
rate_count=$(journalctl --user -u openclaw-gateway --since "${MINUTES} min ago" --no-pager 2>/dev/null \
    | grep -c "rate limit reached")
echo "--- Rate Limit Errors ---"
if [ "$rate_count" -gt 0 ]; then
    echo "⚠️  Total: $rate_count rate limit error(s)"
else
    echo "✅ No rate limit errors"
fi
echo ""

# 3. watchdog.logからsend失敗
echo "--- Watchdog Send Failures ---"
if [ -f /tmp/gokrax-watchdog.log ]; then
    cutoff=$(date -d "${MINUTES} minutes ago" "+%Y-%m-%d %H:%M:%S" 2>/dev/null || date "+%Y-%m-%d %H:%M:%S")
    for agent in $REVIEWERS; do
        recent=$(tail -100 /tmp/gokrax-watchdog.log 2>/dev/null | grep -cE "agent=${agent}.*(timed out|failed)" || true)
        recent=${recent:-0}
        if [ "$recent" -gt 0 ] 2>/dev/null; then
            echo "⚠️  $agent: $recent recent failure(s) in last 100 log lines"
        fi
    done
fi
echo ""

# 4. openclaw agent ping（オプション、--ping フラグ付きの場合のみ）
if [ "$2" = "--ping" ]; then
    echo "--- Ping Test (5s timeout) ---"
    for agent in $REVIEWERS; do
        if timeout 8 openclaw agent --agent "$agent" --message "ping" --timeout 5 >/dev/null 2>&1; then
            echo "✅ $agent: responding"
        else
            echo "❌ $agent: NOT responding"
        fi
    done
fi

echo ""
echo "Done."
