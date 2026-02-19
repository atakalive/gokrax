#!/usr/bin/env python3
"""devbar notify — エージェントへの通知とDiscord投稿

watchdog.pyから呼ばれる。LLM不要。
"""

import subprocess
import json
from pathlib import Path

GATEWAY_TOKEN_PATH = Path.home() / ".openclaw/openclaw.json"
DISCORD_CHANNEL = "1474050582049329213"  # #dev-bar

# agent_id → session_key
AGENTS = {
    "reviewer00": "agent:reviewer00:main",
    "g-reviewer": "agent:g-reviewer:main",
    "c-reviewer": "agent:c-reviewer:main",
    "q-reviewer": "agent:q-reviewer:main",
    "dijkstra":   "agent:dijkstra:main",
}

REVIEWERS = ["g-reviewer", "c-reviewer", "q-reviewer", "dijkstra"]


def send_to_agent(agent_id: str, message: str, timeout: int = 30) -> bool:
    """openclaw agent CLIでメッセージ送信。"""
    try:
        result = subprocess.run(
            ["openclaw", "agent", "--agent", agent_id, "--message", message,
             "--timeout", str(timeout), "--json"],
            capture_output=True, text=True, timeout=timeout + 10,
        )
        return result.returncode == 0
    except Exception:
        return False


def get_bot_token() -> str | None:
    """Discord bot token取得（main-bot）。"""
    import re
    try:
        text = GATEWAY_TOKEN_PATH.read_text()
        text = re.sub(r',\s*([}\]])', r'\1', text)  # trailing comma対策
        data = json.loads(text)
        return data["channels"]["discord"]["accounts"]["main-bot"]["token"]
    except Exception:
        return None


def post_discord(channel_id: str, content: str) -> bool:
    """Discord APIでメッセージ投稿。"""
    import requests
    token = get_bot_token()
    if not token:
        return False
    try:
        resp = requests.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
            json={"content": content},
            timeout=10,
        )
        return resp.status_code in (200, 201)
    except Exception:
        return False


def notify_implementer(agent_id: str, message: str):
    send_to_agent(agent_id, message)


def notify_reviewers(message: str):
    for r in REVIEWERS:
        send_to_agent(r, message)


def notify_discord(message: str):
    post_discord(DISCORD_CHANNEL, message)


def format_review_request(project: str, state: str, batch: list, gitlab: str) -> str:
    """レビュー依頼メッセージを生成。"""
    phase = "設計" if "DESIGN" in state else "コード"
    issues = "\n".join(
        f"- #{i['issue']}: {i.get('title', '')} "
        f"({'commit: ' + i['commit'] if i.get('commit') else 'no commit yet'}) "
        f"https://gitlab.com/{gitlab}/-/issues/{i['issue']}"
        for i in batch
    )
    return (
        f"[devbar] {project}: {phase}レビュー依頼\n\n"
        f"{issues}\n\n"
        f"レビュー結果は `devbar review` コマンドで記録してください。\n"
        f"`glab issue note` でIssueコメントにも残してください。"
    )
