#!/usr/bin/env python3
"""devbar notify — エージェントへの通知とDiscord投稿

watchdog.pyから呼ばれる。LLM不要。
"""

import subprocess
import json
from pathlib import Path

from config import (
    DEVBAR_CLI, DISCORD_CHANNEL, DISCORD_BOT_ACCOUNT, GATEWAY_TOKEN_PATH,
    AGENTS, REVIEWERS,
)


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
    """Discord bot token取得（config.DISCORD_BOT_ACCOUNT）。"""
    import re
    try:
        text = GATEWAY_TOKEN_PATH.read_text()
        text = re.sub(r',\s*([}\]])', r'\1', text)  # trailing comma対策
        data = json.loads(text)
        return data["channels"]["discord"]["accounts"][DISCORD_BOT_ACCOUNT]["token"]
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


def notify_reviewers(project: str, state: str, batch: list, gitlab: str):
    """各レビュアーに個別のコマンド入りメッセージを送信。"""
    for r in REVIEWERS:
        msg = format_review_request(project, state, batch, gitlab, reviewer=r)
        send_to_agent(r, msg)


def notify_discord(message: str):
    post_discord(DISCORD_CHANNEL, message)


def format_review_request(project: str, state: str, batch: list, gitlab: str,
                          reviewer: str) -> str:
    """レビュー依頼メッセージを生成（レビュアーごとにコマンド埋め込み済み）。"""
    phase = "設計" if "DESIGN" in state else "コード"
    sections = []
    for i in batch:
        num = i["issue"]
        title = i.get("title", "")
        commit = i.get("commit")
        commit_str = f"  commit: {commit}" if commit else ""
        url = f"https://gitlab.com/{gitlab}/-/issues/{num}"
        cmd = (
            f"python3 {DEVBAR_CLI} review \\\n"
            f"  --project {project} \\\n"
            f"  --issue {num} \\\n"
            f"  --reviewer {reviewer} \\\n"
            f"  --verdict APPROVE \\\n"
            f'  --summary "ここにレビュー本文"'
        )
        sections.append(
            f"### #{num}: {title}\n"
            f"{url}\n"
            f"{commit_str}\n\n"
            f"```\n{cmd}\n```"
        )

    body = "\n\n".join(sections)
    return (
        f"[devbar] {project}: {phase}レビュー依頼\n\n"
        f"{body}\n\n"
        f"verdict: APPROVE / P0 / P1 から選択。summaryにレビュー本文を書いてください。"
    )
