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
    DEVBAR_CLI, GLAB_BIN, DISCORD_CHANNEL, DISCORD_BOT_TOKEN,
    AGENTS, REVIEW_MODES, MAX_EMBED_CHARS, MAX_DIFF_CHARS, GLAB_TIMEOUT,
    AGENT_SEND_TIMEOUT, DISCORD_POST_TIMEOUT, POST_NEW_COMMAND_WAIT_SEC
)

logger = logging.getLogger("devbar.notify")


def send_to_agent(agent_id: str, message: str, timeout: int = AGENT_SEND_TIMEOUT) -> bool:
    """Gateway chat.send経由でメッセージ送信 (gateway-send.js)。

    collectキュー（デフォルト）により、run中でもabortせずfollowup turnとして処理される。
    stdin経由でメッセージを渡すため、ARG_MAX(128KB)制限を回避。
    chat.sendは改行を保持する。二重送信問題もない。
    """
    if config.DRY_RUN:
        logger.info("[dry-run] send_to_agent skipped (agent=%s)", agent_id)
        return True
    session_key = f"agent:{agent_id}:main"
    try:
        result = subprocess.run(
            ["node", str(GATEWAY_SEND_SCRIPT), session_key],
            input=message,
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
        logger.error("node not found in PATH")
        return False


GATEWAY_SEND_SCRIPT = Path(__file__).resolve().parent / "gateway-send.js"

def send_to_agent_queued(agent_id: str, message: str, timeout: int = AGENT_SEND_TIMEOUT) -> bool:
    """send_to_agent のエイリアス。"""
    return send_to_agent(agent_id, message, timeout)


def ping_agent(agent_id: str, timeout: int = 20) -> bool:
    """Send ping to agent and return True if it responds (returncode==0).

    Protocol: Any response (even "NO_REPLY") counts as alive.
    Timeout or returncode!=0 indicates agent is down.
    In DRY_RUN mode, always returns True.

    Args:
        agent_id: Agent identifier
        timeout: CLI timeout in seconds

    Returns:
        True if agent is alive (returncode==0), False otherwise
    """
    if config.DRY_RUN:
        logger.info("[dry-run] ping_agent skipped (agent=%s)", agent_id)
        return True

    try:
        result = subprocess.run(
            [
                "openclaw", "agent",
                "--agent", agent_id,
                "--message", "ping",
                "--json",
                "--timeout", str(timeout)
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 10,  # subprocess timeout > CLI timeout
        )
        alive = result.returncode == 0
        logger.info(
            "ping_agent %s: %s (rc=%d)",
            agent_id, "alive" if alive else "dead", result.returncode
        )
        return alive
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning("ping_agent %s: dead (%s)", agent_id, e)
        return False


def get_bot_token() -> str | None:
    return DISCORD_BOT_TOKEN


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


def _fetch_commit_diff(commit: str, repo_path: str, base_commit: str | None = None) -> str | None:
    """git diff/show でコミットdiffを取得。

    base_commit が指定されている場合は累積diff (git diff base..commit) を使用。
    未指定の場合は従来通り git show commit を使用。
    """
    try:
        if base_commit:
            cmd = ["git", "-C", repo_path, "diff", f"{base_commit}..{commit}"]
        else:
            cmd = ["git", "-C", repo_path, "show", commit]
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode != 0:
            logger.warning("git diff/show failed (commit=%s, base=%s, rc=%d): %s",
                          commit, base_commit, result.returncode, result.stderr.strip())
            return None
        return result.stdout
    except subprocess.TimeoutExpired:
        logger.warning("git diff/show timed out (commit=%s, base=%s)", commit, base_commit)
        return None
    except FileNotFoundError:
        logger.error("git binary not found in PATH")
        return None


def _check_squash(batch: list, base_commit: str, repo_path: str) -> list[str]:
    """バッチ内の各 issue が squash 済み（1 commit）であることを検証する。

    Returns:
        違反した issue の警告メッセージのリスト。空リストなら全 issue が squash 済み。
    """
    # commit を持つ issue のみ対象
    commits = [(i["issue"], i["commit"]) for i in batch if i.get("commit")]
    if not commits or not base_commit or not repo_path:
        return []

    # トポロジカルソート: base_commit からの rev-list で順序を決定
    try:
        # 全コミットの中で最も新しいもの（最後の子孫）を見つける
        # rev-list は新しい順なので、最初に出るのが最新
        result = subprocess.run(
            ["git", "-C", repo_path, "rev-list", "--topo-order",
             f"{base_commit}..HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode != 0:
            return []  # 検証不能時は続行

        topo_order = result.stdout.strip().split("\n")
        # 各 commit の位置を特定（短縮ハッシュ対応: startswith で比較）
        def topo_index(h):
            for idx, full in enumerate(topo_order):
                if len(h) == 40 and len(full) == 40:
                    if full == h:
                        return idx
                else:
                    # 後方互換: 旧 short hash base_commit との照合。
                    # best-effort: 多重一致時は最初のマッチを返す（不定だが
                    # 旧データ移行期の暫定措置。full SHA 移行後は到達しない）。
                    if full.startswith(h) or h.startswith(full):
                        return idx
            return -1

        # topo_order は新→旧の順なので、index が大きい方が古い → 古い順にソート
        sorted_commits = sorted(commits, key=lambda x: -topo_index(x[1]))
        # topo_index == -1 のものは末尾に（検証スキップ）
        sorted_commits = [c for c in sorted_commits if topo_index(c[1]) >= 0]

    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []  # 検証不能時は続行

    warnings = []
    predecessor = base_commit
    for issue_num, commit in sorted_commits:
        try:
            result = subprocess.run(
                ["git", "-C", repo_path, "rev-list", "--count",
                 f"{predecessor}..{commit}"],
                capture_output=True, text=True, timeout=10, check=False,
            )
            if result.returncode == 0:
                count = int(result.stdout.strip())
                if count > 1:
                    warnings.append(
                        f"Issue #{issue_num}: expected 1 commit after "
                        f"{predecessor[:7]}, got {count}. Squash required."
                    )
        except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
            pass  # 検証不能時はスキップ
        predecessor = commit

    return warnings


def notify_implementer(agent_id: str, message: str):
    if agent_id not in AGENTS:
        logger.error("Unknown agent: %s", agent_id)
        return
    send_to_agent(agent_id, message)


def notify_dispute(
    project: str,
    issue_num: int,
    reviewer: str,
    reason: str,
    gitlab: str = "",
) -> bool:
    """dispute 通知をレビュアーに送信。"""
    if reviewer not in AGENTS:
        logger.warning("notify_dispute: unknown reviewer %s", reviewer)
        return False
    msg = (
        f"【異議申し立て】\n"
        f"{project} #{issue_num} のあなたの P0/P1 判定に対して実装者から異議が出ました。\n\n"
        f"理由: {reason}\n\n"
        f"再評価した上で、以下のいずれかのコマンドで判定を報告してください:\n\n"
        f"# 判定を変更する場合:\n"
        f"python3 {DEVBAR_CLI} review --pj {project} --issue {issue_num} "
        f"--reviewer {reviewer} --verdict APPROVE --force\n"
        f"python3 {DEVBAR_CLI} review --pj {project} --issue {issue_num} "
        f"--reviewer {reviewer} --verdict P2 --summary \"理由\" --force\n"
        f"python3 {DEVBAR_CLI} review --pj {project} --issue {issue_num} "
        f"--reviewer {reviewer} --verdict P1 --summary \"理由\" --force\n\n"
        f"# 現在の判定を維持する場合:\n"
        f"python3 {DEVBAR_CLI} review --pj {project} --issue {issue_num} "
        f"--reviewer {reviewer} --verdict P0 --summary \"維持理由\" --force\n"
        f"python3 {DEVBAR_CLI} review --pj {project} --issue {issue_num} "
        f"--reviewer {reviewer} --verdict P1 --summary \"維持理由\" --force\n\n"
        f"※ --force は必須です（既存レビューの上書きに必要）。\n"
        f"※ 維持する場合も必ずコマンドで明示してください。\n"
        f"※ あなたの現在の verdict に合った --verdict を使ってください。"
    )
    return send_to_agent(reviewer, msg)


def notify_reviewers(project: str, state: str, batch: list, gitlab: str,
                     repo_path: str = "", review_mode: str = "standard",
                     prev_reviews: dict = None, excluded: list[str] = None,
                     base_commit: str | None = None,
                     comment: str = "",
                     already_reset: bool = False):
    """各レビュアーに個別のメッセージを送信。

    review_mode が "skip" の場合は通知をスキップ（自動承認用）。
    バッチ開始時に全レビュアーへ /new を送信してセッションリセット。
    excluded に含まれるレビュアーには通知しない。

    already_reset: True の場合、_reset_reviewers() が既に実行済み（/new 送信 + 待機完了）。
    """
    if prev_reviews is None:
        prev_reviews = {}
    if excluded is None:
        excluded = []

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

    # コードレビュー時: squash 検証
    if "CODE" in state:
        squash_warnings = _check_squash(batch, base_commit, repo_path)
        if squash_warnings:
            for w in squash_warnings:
                logger.warning("Multi-commit (squash needed before merge): %s", w)

    # 各レビュアーにレビュー依頼メッセージ送信（/new はDESIGN_PLAN/IMPL開始時に先行送信済み）
    for r in reviewers:
        if r in excluded:
            logger.info("notify_reviewers: skipping excluded reviewer=%s", r)
            continue
        if r not in AGENTS:
            continue  # 既にログ出力済み
        msg = format_review_request(project, state, batch, gitlab, reviewer=r,
                                    repo_path=repo_path, prev_reviews=prev_reviews,
                                    base_commit=base_commit, comment=comment)
        if not msg:
            logger.info("No pending issues for %s — skipping review request", r)
            continue
        if not send_to_agent(r, msg):
            logger.warning("Failed to send review request to %s", r)
            continue
        # メトリクス記録（Issue #81）
        from pipeline_io import append_metric
        phase = "code" if "CODE" in state else "design"
        review_key = "code_reviews" if "CODE" in state else "design_reviews"
        for i in batch:
            existing = i.get(review_key, {}).get(r, {})
            if existing.get("verdict", "").upper() == "APPROVE":
                continue
            append_metric("review_request", pj=project, issue=i["issue"],
                          phase=phase, reviewer=r)


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
                          reviewer: str, repo_path: str = "",
                          prev_reviews: dict = None,
                          base_commit: str | None = None,
                          comment: str = "") -> str:
    """レビュー依頼メッセージを生成（データ埋め込み + 20000文字制限）。"""
    if prev_reviews is None:
        prev_reviews = {}

    is_code = "CODE" in state
    phase = "コード" if is_code else "設計"
    sections = []
    total_chars = 0
    truncated = False

    for i in batch:
        num = i["issue"]
        title = i.get("title", "")
        commit = i.get("commit")

        # APPROVE済みIssueはスキップ（再レビュー不要）
        review_key = "code_reviews" if is_code else "design_reviews"
        existing = i.get(review_key, {}).get(reviewer, {})
        if existing.get("verdict", "").upper() == "APPROVE":
            continue

        section_parts = [f"### #{num}: {title}\n"]

        # 再レビュー: 前回の指摘を引用 (Issue #35)
        if prev_reviews:
            issue_prev = prev_reviews.get(num, {})
            prev_review = issue_prev.get(reviewer, {})
            if prev_review:
                prev_verdict = prev_review.get("verdict", "")
                prev_summary = prev_review.get("summary", "").strip()
                section_parts[0] = f"### #{num}: {title}（再レビュー — {prev_verdict}対応済み）\n"
                if prev_summary:
                    quoted = "\n".join(f"> {line}" for line in prev_summary.split("\n"))
                    section_parts.insert(1, f"**前回の{prev_verdict}指摘（あなた）:**\n{quoted}\n\n")

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
                orig_len = len(diff)
                if orig_len > MAX_DIFF_CHARS:
                    diff = diff[:MAX_DIFF_CHARS] + f"\n\n... (truncated: {orig_len} chars, limit {MAX_DIFF_CHARS})"
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
            f"--reviewer {reviewer} --verdict <APPROVE|P0|P1|P2> "
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
            "- テストの妥当性を判定\n\n"
            "スコープ制約:\n"
            "- P0/P1 を出す場合、該当コードが今回の diff に含まれることを確認せよ\n"
            "- 前バッチで既に入った変更を現バッチの責任にしない\n"
            "- diff 外で気づいた問題は P2（提案）として報告せよ"
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

    comment_line = f"\nMからの要望: {comment}" if comment else ""
    phase_note = "" if is_code else "\n⚠️ これは設計レビュー DESIGN_REVIEW です。コードやdiffはまだ存在しません。\n"
    return f"[devbar] {project}: {phase}レビュー依頼{comment_line}{phase_note}\n\n{todo_header}\n\n{guidance}\n\n{body}{completion}{truncate_notice}"


# ---------------------------------------------------------------------------
# Spec mode notification formatters (§11)
# ---------------------------------------------------------------------------

def spec_notify_review_start(project: str, rev: str | int, reviewer_count: int) -> str:
    """→ SPEC_REVIEW"""
    return f"[Spec] {project}: rev{rev} レビュー開始 ({reviewer_count}人)"


def spec_notify_review_complete(
    project: str, rev: str | int,
    critical: int, major: int, minor: int, suggestion: int,
) -> str:
    """→ SPEC_REVISE"""
    return f"[Spec] {project}: rev{rev} レビュー完了 — C:{critical} M:{major} m:{minor} s:{suggestion}"


def spec_notify_approved(project: str, rev: str | int) -> str:
    """→ SPEC_APPROVED（通常、M確認待ち）"""
    return f"[Spec] {project}: spec承認 (rev{rev})。`devbar spec continue` でIssue分割へ"


def spec_notify_approved_auto(project: str, rev: str | int) -> str:
    """→ SPEC_APPROVED（auto_continue: 自動進行）"""
    return f"[Spec] {project}: spec承認 (rev{rev}) → Issue分割へ自動進行"


def spec_notify_approved_forced(project: str, rev: str | int, remaining_p1_plus: int) -> str:
    """→ SPEC_APPROVED（強制承認 via `spec approve --force`）"""
    return f"[Spec] ⚠️ {project}: 強制承認 (P1以上 {remaining_p1_plus}件残存)"



def spec_notify_stalled(project: str, rev: str | int, remaining_p1_plus: int) -> str:
    """→ SPEC_STALLED"""
    return f"[Spec] ⏸️ {project}: MAX_CYCLES到達、P1以上 {remaining_p1_plus}件残存"


def spec_notify_review_failed(project: str, rev: str | int) -> str:
    """→ SPEC_REVIEW_FAILED"""
    return f"[Spec] ❌ {project}: 有効レビュー不足"


def spec_notify_paused(project: str, reason: str) -> str:
    """→ SPEC_PAUSED"""
    return f"[Spec] ⏸️ {project}: パイプライン停止 — {reason}"


def spec_notify_revise_done(project: str, rev: str | int, commit: str) -> str:
    """REVISE完了（commit hashあり）。commit は先頭7文字に短縮（§11補足）。"""
    return f"[Spec] {project}: rev{rev} 改訂完了 ({commit[:7]})"


def spec_notify_revise_commit_failed(project: str, rev: str | int) -> str:
    """REVISE完了（git commit失敗）"""
    return f"[Spec] ⚠️ {project}: rev{rev} git commit失敗"


def spec_notify_revise_no_changes(project: str, rev: str | int) -> str:
    """REVISE完了（差分0）→ SPEC_PAUSED"""
    return f"[Spec] ⚠️ {project}: rev{rev} 変更なし（改訂が空）"



def spec_notify_issue_plan_done(project: str, issue_count: int) -> str:
    """→ ISSUE_PLAN完了"""
    return f"[Spec] {project}: {issue_count}件 Issue起票完了"


def spec_notify_queue_plan_done(project: str, batch_count: int) -> str:
    """→ QUEUE_PLAN完了"""
    return f"[Spec] {project}: {batch_count}バッチ キュー生成完了"


def spec_notify_done(project: str) -> str:
    """→ SPEC_DONE"""
    return f"[Spec] ✅ {project}: spec mode完了"


def spec_notify_failure(project: str, kind: str, detail: str = "") -> str:
    """失敗系通知（汎用）。kind: "YAMLパース失敗", "送信失敗", "git push失敗", "Issue起票失敗" 等。"""
    suffix = f" — {detail}" if detail else ""
    return f"[Spec] ❌ {project}: {kind}{suffix}"


def spec_notify_self_review_failed(project: str, failed_count: int) -> str:
    """セルフレビュー差し戻し通知。"""
    return f"🔁 [{project}] セルフレビュー: {failed_count}件の問題検出。implementer に差し戻し"
