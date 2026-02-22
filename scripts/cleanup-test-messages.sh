#!/usr/bin/env bash
# #dev-bar から [test-pj] で始まるメッセージを削除
# 使い方: bash scripts/cleanup-test-messages.sh

set -euo pipefail

CHANNEL_ID="1474050582049329213"
CONFIG="$HOME/.openclaw/openclaw.json"
# trailing comma対策
TOKEN=$(python3 -c "
import json, re
text = open('$CONFIG').read()
text = re.sub(r',\s*([}\]])', r'\1', text)
data = json.loads(text)
print(data['channels']['discord']['accounts']['kaneko-discord']['token'])
")

deleted=0
# 最新100件を取得してフィルタ（API上限）
messages=$(curl -s -H "Authorization: Bot $TOKEN" \
  "https://discord.com/api/v10/channels/$CHANNEL_ID/messages?limit=100")

echo "$messages" | python3 -c "
import sys, json
msgs = json.load(sys.stdin)
for m in msgs:
    content = m.get('content', '')
    if content.startswith('[test-pj]'):
        print(m['id'])
" | while read -r msg_id; do
  resp=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE \
    -H "Authorization: Bot $TOKEN" \
    "https://discord.com/api/v10/channels/$CHANNEL_ID/messages/$msg_id")
  if [ "$resp" = "204" ]; then
    echo "Deleted: $msg_id"
    ((deleted++)) || true
  else
    echo "Failed ($resp): $msg_id"
  fi
  sleep 0.5  # rate limit対策
done

echo "Done."
