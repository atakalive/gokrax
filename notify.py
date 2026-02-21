#!/usr/bin/env python3
"""devbar notify — エージェントへの通知とDiscord投稿

watchdog.pyから呼ばれる。LLM不要。
"""

import logging
import subprocess
import json
from pathlib import Path

import requests

from config import (
    DEVBAR_CLI, GLAB_BIN, DISCORD_CHANNEL, DISCORD_BOT_ACCOUNT, GATEWAY_TOKEN_PATH,
    AGENTS, REVIEWERS,
    AGENT_SEND_TIMEOUT, DISCORD_POST_TIMEOUT,
)

logger = logging.getLogger("devbar.notify")


def send_to_agent(agent_id: str, message: str, timeout: int = AGENT_SEND_TIMEOUT) -> bool:
    """openclaw agent CLIでメッセージ送信。"""
    try:
        result = subprocess.run(
            ["openclaw", "agent", "--agent", agent_id, "--message", message,
             "--timeout", str(timeout), "--json"],
            capture_output=True, text=True, timeout=timeout + 10,
        )
        if result.returncode != 0:
            logger.warning("agent send failed (rc=%d, agent=%s): %s",
                          result.returncode, agent_id, result.stderr.strip())
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        logger.warning("agent send timed out (agent=%s, timeout=%ds)", agent_id, timeout)
        return False
    except FileNotFoundError:
        logger.error("openclaw CLI not found in PATH")
        return False


def get_bot_token() -> str | None:
    """Discord bot token取得。失敗時はログ出力してNone返却。"""
    import re
    try:
        text = GATEWAY_TOKEN_PATH.read_text()
    except FileNotFoundError:
        logger.error("Gateway config not found: %s", GATEWAY_TOKEN_PATH)
        return None
    except OSError as e:
        logger.error("Cannot read gateway config: %s", e)
        return None

    # trailing comma 除去
    text = re.sub(r',\s*([}\]])', r'\1', text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in gateway config: %s", e)
        return None

    try:
        return data["channels"]["discord"]["accounts"][DISCORD_BOT_ACCOUNT]["token"]
    except KeyError as e:
        logger.error("Discord bot token key not found: %s (account=%s)", e, DISCORD_BOT_ACCOUNT)
        return None


def post_discord(channel_id: str, content: str) -> bool:
    """Discord APIでメッセージ投稿。"""
    token = get_bot_token()
    if not token:
        return False
    try:
        resp = requests.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
            json={"content": content},
            timeout=DISCORD_POST_TIMEOUT,
        )
        if resp.status_code not in (200, 201):
            logger.warning("Discord post failed (status=%d): %s", resp.status_code, resp.text[:200])
        return resp.status_code in (200, 201)
    except requests.RequestException as e:
        logger.warning("Discord post error: %s", e)
        return False


def notify_implementer(agent_id: str, message: str):
    if agent_id not in AGENTS:
        logger.error("Unknown agent: %s", agent_id)
        return
    send_to_agent(agent_id, message)


def notify_reviewers(project: str, state: str, batch: list, gitlab: str,
                     repo_path: str = ""):
    """各レビュアーに個別のコマンド入りメッセージを送信。"""
    for r in REVIEWERS:
        if r not in AGENTS:
            logger.error("Unknown reviewer: %s", r)
            continue
        msg = format_review_request(project, state, batch, gitlab, reviewer=r,
                                    repo_path=repo_path)
        send_to_agent(r, msg)


def notify_discord(message: str):
    post_discord(DISCORD_CHANNEL, message)


def format_review_request(project: str, state: str, batch: list, gitlab: str,
                          reviewer: str, repo_path: str = "") -> str:
    """レビュー依頼メッセージを生成（レビュアーごとにコマンド埋め込み済み）。"""
    is_code = "CODE" in state
    phase = "コード" if is_code else "設計"
    sections = []
    for i in batch:
        num = i["issue"]
        title = i.get("title", "")
        commit = i.get("commit")
        glab_show = f"{GLAB_BIN} issue show {num} -R {gitlab}"
        cmd = (
            f"python3 {DEVBAR_CLI} review \\\n"
            f"  --project {project} \\\n"
            f"  --issue {num} \\\n"
            f"  --reviewer {reviewer} \\\n"
            f"  --verdict APPROVE \\\n"
            f"  --summary $'ここにレビュー本文\\n本文2行目\\n本文3行目...'"
        )

        if is_code and commit and repo_path:
            # コードレビュー: git diff情報付き
            sections.append(
                f"### #{num}: {title}\n"
                f"Issue詳細: `{glab_show}`\n"
                f"変更確認:\n"
                f"  `git -C {repo_path} show --stat {commit}`  # 変更ファイル一覧\n"
                f"  `git -C {repo_path} show {commit}`  # diff全文\n\n"
                f"```\n{cmd}\n```"
            )
        else:
            # 設計レビュー: Issue本文のみ
            sections.append(
                f"### #{num}: {title}\n"
                f"Issue取得: `{glab_show}`\n\n"
                f"```\n{cmd}\n```"
            )

    body = "\n\n".join(sections)

    if is_code:
        guidance = (
            "レビュー観点:\n"
            "- 設計レビューで承認された仕様通りに実装されているか\n"
            "- バグ、エッジケース、型ヒントの欠落\n"
            "- テストがあれば妥当性を確認\n\n"
            "verdict: APPROVE / P0 / P1 から選択。summaryにレビュー本文を書いてください。"
        )
    else:
        guidance = "verdict: APPROVE / P0 / P1 から選択。summaryにレビュー本文を書いてください。"

    return f"[devbar] {project}: {phase}レビュー依頼\n\n{body}\n\n{guidance}"

def format_impl_instruction(project: str, batch: list, gitlab: str) -> str:
    """実装指示メッセージを生成（CC モデル指定付き）。"""
    from config import CC_MODEL_PLAN, CC_MODEL_IMPL
    issues = ", ".join(f"#{i['issue']}" for i in batch)
    return (
        f"[devbar] {project}: 実装フェーズ開始\n\n"
        f"対象Issue: {issues}\n"
        f"GitLab: https://gitlab.com/{gitlab}\n\n"
        f"CC Plan: `claude --model {CC_MODEL_PLAN}` (設計確認)\n"
        f"CC Impl: `claude --model {CC_MODEL_IMPL}` (実装)\n"
    )
