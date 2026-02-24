#!/usr/bin/env python3
"""devbar notify — エージェントへの通知とDiscord投稿

watchdog.pyから呼ばれる。LLM不要。
"""

import logging
import subprocess
import json
from pathlib import Path

import requests

import config
from config import (
    DEVBAR_CLI, GLAB_BIN, DISCORD_CHANNEL, DISCORD_BOT_ACCOUNT, GATEWAY_TOKEN_PATH,
    AGENTS, REVIEW_MODES, MAX_EMBED_CHARS, GLAB_TIMEOUT,
    AGENT_SEND_TIMEOUT, DISCORD_POST_TIMEOUT, POST_NEW_COMMAND_WAIT_SEC
)

logger = logging.getLogger("devbar.notify")


GATEWAY_SEND_SCRIPT = Path(__file__).resolve().parent / "gateway-send.js"


def send_to_agent(agent_id: str, message: str, timeout: int = AGENT_SEND_TIMEOUT) -> bool:
    """Gateway chat.send 経由でメッセージ送信（キュー対応、run中でもabortしない）。"""
    if config.DRY_RUN:
        logger.info("[dry-run] send_to_agent skipped (agent=%s)", agent_id)
        return True
    session_key = f"agent:{agent_id}:main"
    try:
        result = subprocess.run(
            ["node", str(GATEWAY_SEND_SCRIPT), session_key, message],
            capture_output=True, text=True, timeout=timeout + 10,
        )
        if result.returncode != 0:
            logger.warning("gateway-send failed (rc=%d, agent=%s): %s",
                          result.returncode, agent_id, result.stderr.strip())
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        logger.warning("gateway-send timed out (agent=%s, timeout=%ds)", agent_id, timeout)
        return False
    except FileNotFoundError:
        logger.error("node not found in PATH")
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


def post_discord(channel_id: str, content: str, retries: int = 3) -> str | None:
    """Discord APIでメッセージ投稿。2000文字超は自動分割。失敗時はリトライ。

    成功時は最後のmessage_id、全リトライ失敗時はNone。
    """
    if config.DRY_RUN:
        logger.info("[dry-run] post_discord skipped (channel=%s)", channel_id)
        return None
    token = get_bot_token()
    if not token:
        return None

    import time as _time
    chunks = _split_message(content, 2000)
    last_id = None
    for chunk in chunks:
        chunk_id = None
        for attempt in range(retries):
            try:
                resp = requests.post(
                    f"https://discord.com/api/v10/channels/{channel_id}/messages",
                    headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
                    json={"content": chunk},
                    timeout=DISCORD_POST_TIMEOUT,
                )
                if resp.status_code in (200, 201):
                    chunk_id = resp.json().get("id")
                    break
                logger.warning("Discord post failed (attempt %d/%d, status=%d): %s",
                              attempt + 1, retries, resp.status_code, resp.text[:200])
            except requests.RequestException as e:
                logger.warning("Discord post error (attempt %d/%d): %s", attempt + 1, retries, e)
            if attempt < retries - 1:
                _time.sleep(2)
        if chunk_id is None:
            logger.error("Discord post failed after %d attempts", retries)
            return None
        last_id = chunk_id
    return last_id


def _split_message(text: str, limit: int = 2000) -> list[str]:
    """テキストを改行境界で limit 文字以下に分割する。"""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # 改行で切れる位置を探す
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit  # 改行がなければ強制切断
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


def fetch_issue_body(issue_num: int, gitlab: str) -> str | None:
    """GitLab Issue本文を取得（glab issue show --output json）。"""
    try:
        result = subprocess.run(
            [GLAB_BIN, "issue", "show", str(issue_num), "--output", "json", "-R", gitlab],
            capture_output=True, text=True, timeout=GLAB_TIMEOUT, check=False,
        )
        if result.returncode != 0:
            logger.warning("glab issue show failed (issue=%d, rc=%d): %s",
                          issue_num, result.returncode, result.stderr.strip())
            return None
        data = json.loads(result.stdout)
        return data.get("description", "")
    except subprocess.TimeoutExpired:
        logger.warning("glab issue show timed out (issue=%d)", issue_num)
        return None
    except json.JSONDecodeError as e:
        logger.warning("glab issue show invalid JSON (issue=%d): %s", issue_num, e)
        return None
    except FileNotFoundError:
        logger.error("glab binary not found: %s", GLAB_BIN)
        return None


def _fetch_commit_diff(commit: str, repo_path: str) -> str | None:
    """git show でコミットdiffを取得。"""
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "show", commit],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode != 0:
            logger.warning("git show failed (commit=%s, rc=%d): %s",
                          commit, result.returncode, result.stderr.strip())
            return None
        return result.stdout
    except subprocess.TimeoutExpired:
        logger.warning("git show timed out (commit=%s)", commit)
        return None
    except FileNotFoundError:
        logger.error("git binary not found in PATH")
        return None


def notify_implementer(agent_id: str, message: str):
    if agent_id not in AGENTS:
        logger.error("Unknown agent: %s", agent_id)
        return
    send_to_agent(agent_id, message)


def notify_reviewers(project: str, state: str, batch: list, gitlab: str,
                     repo_path: str = "", review_mode: str = "standard"):
    """各レビュアーに個別のメッセージを送信。

    review_mode が "skip" の場合は通知をスキップ（自動承認用）。
    バッチ開始時に全レビュアーへ /new を送信してセッションリセット。
    """
    # review_mode 検証
    if review_mode not in REVIEW_MODES:
        logger.error("Invalid review_mode: %s, defaulting to 'standard'", review_mode)
        review_mode = "standard"

    mode_config = REVIEW_MODES[review_mode]
    reviewers = mode_config["members"]

    # "skip" モード: 通知なし（watchdog が自動承認を処理）
    if review_mode == "skip":
        logger.info("[review_mode=skip] Skipping reviewer notifications for %s", project)
        return

    # 各レビュアーにレビュー依頼メッセージ送信（/new はDESIGN_PLAN/IMPL開始時に先行送信済み）
    for r in reviewers:
        if r not in AGENTS:
            continue  # 既にログ出力済み
        msg = format_review_request(project, state, batch, gitlab, reviewer=r,
                                    repo_path=repo_path)
        if not msg:
            logger.info("No pending issues for %s — skipping review request", r)
            continue
        if not send_to_agent(r, msg):
            logger.warning("Failed to send review request to %s", r)


def notify_discord(message: str):
    post_discord(DISCORD_CHANNEL, message)


def fetch_discord_latest(channel_id: str, limit: int = 10) -> list[dict]:
    """チャンネルの最新メッセージをlimit件取得（新しい順）。

    Args:
        channel_id: Discord channel ID
        limit: 取得件数 (default: 10, max: 100)

    Returns:
        list of message objects (newest first), or [] on error
    """
    token = get_bot_token()
    if not token:
        return []
    try:
        resp = requests.get(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {token}"},
            params={"limit": limit},
            timeout=DISCORD_POST_TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.json()
        logger.warning("Discord fetch failed (status=%d)", resp.status_code)
        return []
    except requests.RequestException as e:
        logger.warning("Discord fetch error: %s", e)
        return []


def fetch_discord_replies(channel_id: str, after_message_id: str) -> list[dict]:
    """指定メッセージ以降の全メッセージを取得。"""
    token = get_bot_token()
    if not token:
        return []
    try:
        resp = requests.get(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {token}"},
            params={"after": after_message_id, "limit": 50},
            timeout=DISCORD_POST_TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.json()
        logger.warning("Discord fetch failed (status=%d)", resp.status_code)
        return []
    except requests.RequestException as e:
        logger.warning("Discord fetch error: %s", e)
        return []


def format_review_request(project: str, state: str, batch: list, gitlab: str,
                          reviewer: str, repo_path: str = "") -> str:
    """レビュー依頼メッセージを生成（データ埋め込み + 20000文字制限）。"""
    is_code = "CODE" in state
    phase = "コード" if is_code else "設計"
    sections = []
    total_chars = 0
    truncated = False

    for i in batch:
        num = i["issue"]
        title = i.get("title", "")
        commit = i.get("commit")

        # APPROVE/P1済みIssueはスキップ（再レビュー不要）
        review_key = "code_reviews" if is_code else "design_reviews"
        existing = i.get(review_key, {}).get(reviewer, {})
        if existing.get("verdict", "").upper() in ("APPROVE", "P1"):
            continue

        section_parts = [f"### #{num}: {title}\n"]

        # Issue本文を取得して埋め込み
        issue_body = fetch_issue_body(num, gitlab)
        if issue_body:
            section_parts.append(f"**Issue本文:**\n```\n{issue_body}\n```\n")
        else:
            # 取得失敗時: fallbackコマンド
            section_parts.append(f"Issue詳細: `{GLAB_BIN} issue show {num} -R {gitlab}`\n")

        # コードレビュー: git diff埋め込み
        if is_code and commit and repo_path:
            diff = _fetch_commit_diff(commit, repo_path)
            if diff:
                section_parts.append(f"**変更内容:**\n```diff\n{diff}\n```\n")
            else:
                # 取得失敗時: fallbackコマンド
                section_parts.append(
                    f"変更確認:\n"
                    f"  `git -C {repo_path} show --stat {commit}`\n"
                    f"  `git -C {repo_path} show {commit}`\n\n"
                )

        section_text = "".join(section_parts)

        # 文字数制限チェック
        if total_chars + len(section_text) > MAX_EMBED_CHARS:
            sections.append(
                f"### #{num}: {title}\n"
                f"**(truncated)** 文字数制限のため省略。以下のコマンドで確認してください:\n"
                f"  Issue: `{GLAB_BIN} issue show {num} -R {gitlab}`\n"
                + (f"  Diff: `git -C {repo_path} show {commit}`\n" if commit else "")
            )
            truncated = True
        else:
            sections.append(section_text)
            total_chars += len(section_text)

    # 全IssueがAPPROVE済みなら空レビュー依頼を返さない
    if not sections:
        return ""

    # --- TODOチェックリスト（冒頭） ---
    pending_issues = []
    pending_cmds = []
    for i in batch:
        num = i["issue"]
        title = i.get("title", "")
        review_key = "code_reviews" if is_code else "design_reviews"
        existing = i.get(review_key, {}).get(reviewer, {})
        if existing.get("verdict", "").upper() in ("APPROVE", "P1"):
            continue
        pending_issues.append(f"□ #{num}: {title}")
        pending_cmds.append(
            f"python3 {DEVBAR_CLI} review --project {project} --issue {num} "
            f"--reviewer {reviewer} --verdict <APPROVE|P0|P1> "
            f"--summary $'レビュー本文\n2行目\n3行目..'"
        )

    todo_header = (
        f"【タスク: {len(pending_issues)}件 — 全て完了するまで止めるな】\n"
        + "\n".join(pending_issues)
    )

    body = "\n\n".join(sections)

    if is_code:
        guidance = (
            "レビュー観点:\n"
            "- 設計レビューで承認された仕様通りに実装されているか\n"
            "- バグ、エッジケース、型ヒントの欠落\n"
            "- テストの妥当性を判定"
        )
    else:
        guidance = (
            "レビュー観点:\n"
            "- 数理的に精確か（厳しく検証せよ）\n"
            "- Issue本文の仕様が明確か、実装可能か\n"
            "- 矛盾やエッジケースがないか、思わぬ落とし穴がないか"
        )

    # --- 完了コマンド一覧（末尾） ---
    cmds_block = "\n".join(pending_cmds)
    completion = (
        f"\n\n【完了コマンド — 全Issue分を実行すること】\n"
        f"```\n{cmds_block}\n```\n"
        f"⚠️ 全Issueのreviewコマンドを実行するまでタスク未完了。途中で止めるな。"
    )

    truncate_notice = "\n\n**注意:** 一部のデータが文字数制限により省略されています。" if truncated else ""

    return f"[devbar] {project}: {phase}レビュー依頼\n\n{todo_header}\n\n{guidance}\n\n{body}{completion}{truncate_notice}"
