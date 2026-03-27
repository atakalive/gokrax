#!/usr/bin/env python3
"""gokrax notify — エージェントへの通知とDiscord投稿

watchdog.pyから呼ばれる。LLM不要。
"""

import logging
import os
import re
import subprocess
import json
import time
import uuid
from pathlib import Path

import requests

import config
from config import (
    GOKRAX_CLI, GLAB_BIN, DISCORD_CHANNEL, DISCORD_BOT_TOKEN,
    AGENTS, REVIEW_MODES, MAX_DIFF_CHARS, GLAB_TIMEOUT,
    AGENT_SEND_TIMEOUT, DISCORD_POST_TIMEOUT, POST_NEW_COMMAND_WAIT_SEC,
    MAX_CLI_ARG_BYTES, REVIEW_FILE_DIR, REVIEW_FILE_WRITE_RETRIES,
    REVIEW_FILE_WRITE_RETRY_DELAY,
)

logger = logging.getLogger("gokrax.notify")


def load_skills(agent_name: str, project: str = "", phase: str = "") -> str:
    """指定エージェント・プロジェクト・フェーズに紐付けられたスキルファイルを読み込み、結合して返す。

    Args:
        agent_name: AGENT_SKILLS のキー
        project: PROJECT_SKILLS のキー（空文字列の場合はプロジェクト別スキルなし）
        phase: "design" または "code"（空文字列の場合はスキル注入なし）

    Returns:
        スキル内容を結合した文字列。スキルがない場合は空文字列。
        - phase が空の場合 → 空文字列
        - AGENT_SKILLS にキーがない場合 → 空文字列
        - スキル名が SKILLS に存在しない場合 → warning を出してスキップ
        - ファイル読み込みに失敗した場合 → warning を出してスキップ
        - 結合結果が MAX_SKILL_CHARS を超える場合 → warning を出して切り詰め
    """
    if not phase:
        return ""

    # エージェント別スキル
    agent_phase_skills = config.AGENT_SKILLS.get(agent_name, {})
    if isinstance(agent_phase_skills, list):
        # 旧形式（list[str]）: 全フェーズに適用しつつ deprecation warning
        logger.warning(
            "load_skills: AGENT_SKILLS[%r] is list (deprecated). "
            "Migrate to dict[str, list[str]] format: {\"design\": [...], \"code\": [...]}",
            agent_name,
        )
        a: list[str] = agent_phase_skills
    elif isinstance(agent_phase_skills, dict):
        a = agent_phase_skills.get(phase, [])
    else:
        a = []

    # プロジェクト別スキル
    p: list[str] = config.PROJECT_SKILLS.get(project, {}).get(phase, []) if project else []

    # 和集合（重複排除）、順序は安定させる（元リスト順を維持）
    seen: set[str] = set()
    skill_names: list[str] = []
    for name in (*a, *p):
        if name not in seen:
            seen.add(name)
            skill_names.append(name)

    if not skill_names:
        return ""

    parts: list[str] = []
    for name in skill_names:
        path_str = config.SKILLS.get(name)
        if path_str is None:
            logger.warning("load_skills: unknown skill '%s' for agent '%s'", name, agent_name)
            continue
        try:
            content = Path(path_str).read_text(encoding="utf-8").rstrip("\n")
            parts.append(f"--- skill: {name} ---\n{content}")
        except OSError as e:
            logger.warning("load_skills: failed to read '%s': %s", path_str, e)

    if not parts:
        return ""

    block = "<skills>\n" + "\n\n".join(parts) + "\n</skills>"

    _OPENING_TAG = "<skills>\n"
    _CLOSING_TAG = "\n</skills>"
    _MIN_SKILL_CHARS = len(_OPENING_TAG) + len(_CLOSING_TAG)
    # 不変条件: MAX_SKILL_CHARS >= _MIN_SKILL_CHARS（開始タグ+終了タグの長さ）。
    # これより小さい値を設定した場合、切り詰めではなく空文字列を返す。
    if len(block) > config.MAX_SKILL_CHARS:
        logger.warning(
            "load_skills: skill block for '%s' exceeds %d chars (%d), truncating",
            agent_name, config.MAX_SKILL_CHARS, len(block),
        )
        if config.MAX_SKILL_CHARS < _MIN_SKILL_CHARS:
            return ""
        # 切り詰め後も closing tag を含めて MAX_SKILL_CHARS 以下を保証
        content_limit = config.MAX_SKILL_CHARS - len(_CLOSING_TAG)
        block = block[:content_limit] + _CLOSING_TAG

    return block


def review_command(project: str, issue: int, reviewer: str, round_num: int | None = None) -> str:
    """レビュー報告コマンド文字列を生成する。単一ソース。"""
    round_arg = f' --round {round_num}' if round_num is not None else ''
    cmd = (
        f'python3 {GOKRAX_CLI} review'
        f' --project {project}'
        f' --issue {issue}'
        f'{round_arg}'
        f' --reviewer {reviewer}'
        f' --verdict <APPROVE/P0/P1/P2>'
        f' --summary "..."'
    )
    return cmd


def _gateway_chat_send_cli(params_json: str, timeout: int) -> bool:
    """openclaw gateway call CLI 経由で chat.send を送信する。

    CLI が device identity と全 auth mode を内部で処理する。
    MAX_CLI_ARG_BYTES (128KB) 未満のメッセージ専用。

    Args:
        params_json: JSON 文字列（sessionKey, message, idempotencyKey）
        timeout: 秒単位のタイムアウト

    Returns:
        True: chat.send 成功, False: 失敗
    """
    try:
        result = subprocess.run(
            [
                "openclaw", "gateway", "call", "chat.send",
                "--params", params_json,
                "--json",
                "--timeout", str(timeout * 1000),
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 5,  # CLI timeout + margin
        )
        if result.returncode != 0:
            logger.warning("openclaw gateway call failed (rc=%d): %s",
                          result.returncode, result.stderr.strip()[:200])
            return False
        resp = json.loads(result.stdout)
        return resp.get("ok", False) or resp.get("status") == "started"
    except FileNotFoundError:
        logger.error("openclaw not found in PATH")
        return False
    except subprocess.TimeoutExpired:
        logger.warning("openclaw gateway call timed out (%ds)", timeout)
        return False
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("openclaw gateway call error: %s", e)
        return False


def _gateway_chat_send(session_key: str, message: str, timeout: int) -> bool:
    """Gateway 経由で chat.send を送信する（CLI経由のみ）。

    MAX_CLI_ARG_BYTES 未満の params_json 専用。
    それ以上のメッセージは呼び出し元でファイル外部化すること。
    """
    params_json = json.dumps({
        "sessionKey": session_key,
        "message": message,
        "idempotencyKey": str(uuid.uuid4()),
    })
    return _gateway_chat_send_cli(params_json, timeout)


def _send_to_agent_openclaw(agent_id: str, message: str, timeout: int = AGENT_SEND_TIMEOUT) -> bool:
    """OpenClaw-specific send via gateway chat.send."""
    if config.DRY_RUN:
        logger.info("[dry-run] send_to_agent skipped (agent=%s)", agent_id)
        return True
    session_key = f"agent:{agent_id}:main"
    return _gateway_chat_send(session_key, message, timeout)


def send_to_agent(agent_id: str, message: str, timeout: int = AGENT_SEND_TIMEOUT) -> bool:
    """Send message to agent, dispatching to the selected backend.

    collectキュー（デフォルト）により、run中でもabortせずfollowup turnとして処理される。
    改行を保持する。
    """
    from engine.backend import send as _dispatch_send
    return _dispatch_send(agent_id, message, timeout)


def send_to_agent_queued(agent_id: str, message: str, timeout: int = AGENT_SEND_TIMEOUT) -> bool:
    """send_to_agent のエイリアス。"""
    return send_to_agent(agent_id, message, timeout)


def _write_review_file(
    project: str,
    reviewer: str,
    content: str,
) -> Path | None:
    """レビューデータをファイルに書き出す。

    Args:
        project: プロジェクト名
        reviewer: レビュアー名
        content: レビュー依頼メッセージの全文

    Returns:
        書き出し先のPathオブジェクト。全リトライ失敗時はNone。

    ファイルパス: /tmp/gokrax-review/{project}-{reviewer}-{uuid4}.md
    プロジェクト名の正規化: スラッシュ・空白を '-' に置換してパストラバーサルを防止する。
        sanitized = re.sub(r'[/\\\\\\s]', '-', project)
    リトライ: REVIEW_FILE_WRITE_RETRIES回、間隔REVIEW_FILE_WRITE_RETRY_DELAY秒。
    想定障害: NFS/CIFS上の一時的なI/Oエラー、WSL環境でのファイルシステム遅延。
    ディスクフルのような持続的障害ではリトライは無意味だが、3回×2秒は実害のないコストなので
    一律リトライとし、障害種別の判定は行わない。
    """
    sanitized = re.sub(r'[/\\\s]', '-', project)
    os.makedirs(REVIEW_FILE_DIR, exist_ok=True)
    file_path = REVIEW_FILE_DIR / f"{sanitized}--{reviewer}-{uuid.uuid4()}.md"
    for attempt in range(REVIEW_FILE_WRITE_RETRIES):
        try:
            file_path.write_text(content, encoding="utf-8")
            return file_path
        except OSError as e:
            logger.warning("Review file write failed (attempt %d/%d): %s",
                          attempt + 1, REVIEW_FILE_WRITE_RETRIES, e)
            if attempt < REVIEW_FILE_WRITE_RETRIES - 1:
                time.sleep(REVIEW_FILE_WRITE_RETRY_DELAY)
    logger.error("Failed to write review file after %d attempts: %s",
                REVIEW_FILE_WRITE_RETRIES, file_path)
    return None


def _build_file_review_message(
    project: str,
    is_code: bool,
    reviewer: str,
    file_path: Path,
    batch: list,
    round_num: int | None,
    *,
    skip_skills: bool = False,
) -> str:
    """ファイル外部化時のレビュー依頼メッセージを生成する。

    Args:
        project: プロジェクト名
        is_code: True=コードレビュー、False=設計レビュー
        reviewer: レビュアー名
        file_path: 書き出し先ファイルパス
        batch: バッチ内Issueリスト（未APPROVE分のIssue番号を抽出するため）
        round_num: 現在のラウンド番号（--round引数用）
    """
    phase = "code" if is_code else "design"
    review_key = "code_reviews" if is_code else "design_reviews"

    # 未APPROVEのIssueを抽出
    pending_issues = []
    for i in batch:
        existing = i.get(review_key, {}).get(reviewer, {})
        if existing.get("verdict", "").upper() == "APPROVE":
            continue
        pending_issues.append(i)

    n = len(pending_issues)

    # 各Issueのreviewコマンド生成
    review_cmds = []
    for i in pending_issues:
        cmd = review_command(project, i["issue"], reviewer, round_num)
        review_cmds.append(cmd)

    cmds_block = "\n".join(review_cmds)

    from messages import render
    review_module = "dev.code_review" if is_code else "dev.design_review"
    msg = render(review_module, "file_review_request",
        project=project, n=n, file_path=str(file_path), cmds_block=cmds_block,
    )

    # スキルブロック付与（NPASS では初回レビューで注入済みのためスキップ）
    if not skip_skills:
        skill_phase = "code" if is_code else "design"
        skill_block = load_skills(reviewer, project, skill_phase)
        if skill_block:
            msg = f"{skill_block}\n\n{msg}"

    return msg


def _build_npass_review_message(
    project: str,
    state: str,
    batch: list,
    reviewer: str,
    round_num: int | None = None,
    comment: str = "",
    gitlab: str = "",
) -> str:
    """NPASS レビュー依頼メッセージを生成。Issue本文・diff は含めない。"""
    is_code = "CODE" in state
    review_key = "code_reviews" if is_code else "design_reviews"

    # バッチ内の対象 Issue から pass / target_pass を取得
    pass_nums: list[int] = []
    target_passes: list[int] = []
    pending_issues: list[dict] = []
    for i in batch:
        entry = i.get(review_key, {}).get(reviewer, {})
        p = entry.get("pass", 1)
        tp = entry.get("target_pass", 1)
        if p < tp:
            pass_nums.append(p)
            target_passes.append(tp)
            pending_issues.append(i)

    if not pending_issues:
        return ""

    # バッチ内パス均一性チェック（防御的フォールバック）
    if len(set(pass_nums)) > 1 or len(set(target_passes)) > 1:
        logger.warning(
            "NPASS pass numbers not uniform for reviewer %s: passes=%s, targets=%s",
            reviewer, pass_nums, target_passes,
        )
    pass_num = min(pass_nums) + 1  # next pass number
    target_pass = min(target_passes) if target_passes else 1

    # TODO チェックリスト
    todo_lines: list[str] = []
    review_cmds: list[str] = []
    for i in pending_issues:
        num = i["issue"]
        title = i.get("title", "")
        todo_lines.append(f"□ #{num}: {title}")
        cmd = review_command(project, num, reviewer, round_num)
        review_cmds.append(cmd)

    todo_header = (
        f"[Task: {len(pending_issues)} items — do not stop until all are completed]\n"
        + "\n".join(todo_lines)
        + "\n\n⚠️ Anonymous review: do not include your name or agent name in --summary."
    )

    cmds_block = "\n".join(review_cmds)
    completion = (
        f"\n\n[Completion commands — execute for all Issues]\n"
        f"```\n{cmds_block}\n```\n"
        f"⚠️ Task is incomplete until review commands are executed for all Issues. Do not stop midway."
    )

    from config import OWNER_NAME
    comment_line = f"\n{OWNER_NAME}'s request: {comment}" if comment else ""

    # Issue 内容の再取得コマンド（リテラル N ではなく具体的な Issue 番号）
    gitlab_ref = f" -R {gitlab}" if gitlab else ""
    issue_nums = [i["issue"] for i in pending_issues]
    if is_code:
        saved_path = _load_npass_review_file_path(project, reviewer)
        if saved_path:
            from shlex import quote as _shquote
            refresher_cmds = f"Full review content from previous pass: `cat {_shquote(saved_path)}`"
        else:
            refresher_cmds = "Re-execute files/commands referenced in the previous pass to verify."
    else:
        view_cmds = [f"`glab issue view {n}{gitlab_ref}`" for n in issue_nums]
        note_cmds = [f"`glab issue note-list {n}{gitlab_ref}`" for n in issue_nums]
        refresher_cmds = (
            "Check Issue body: " + ", ".join(view_cmds) + "\n"
            "Check previous review comments: " + ", ".join(note_cmds)
        )

    from messages import render
    review_module = "dev.code_review_npass" if is_code else "dev.design_review_npass"
    msg = render(review_module, "review_request",
        project=project, todo_header=todo_header, completion=completion,
        pass_num=pass_num, target_pass=target_pass, comment_line=comment_line,
        refresher_cmds=refresher_cmds,
    )

    return msg


def mask_agent_name(name: str, reviewer_number_map: dict[str, int] | None = None) -> str:
    """MASK_AGENT_NAMES が True の場合、エージェント名を 'Reviewer N' に変換する。

    reviewer_number_map が渡された場合はバッチ固有の番号を使用する。
    フォールバック: REVIEWERS リストにおけるインデックス + 1。
    REVIEWERS に含まれない名前はそのまま返す（M, dispute 等）。
    """
    from config import MASK_AGENT_NAMES
    if not MASK_AGENT_NAMES:
        return name
    if reviewer_number_map and name in reviewer_number_map:
        return f"Reviewer {reviewer_number_map[name]}"
    # フォールバック: reviewer_number_map が None または name が未登録の場合。
    # 防御的コード。正常系では reviewer_number_map が常に渡されるはず。
    # ここに到達した場合は番号の一貫性が保証されないため警告を出す。
    import logging
    logging.getLogger(__name__).warning(
        "mask_agent_name: reviewer_number_map missing or incomplete for %s, "
        "falling back to REVIEWERS index", name
    )
    from config import REVIEWERS
    try:
        idx = REVIEWERS.index(name)
        return f"Reviewer {idx + 1}"
    except ValueError:
        return name


def resolve_reviewer_arg(
    name_or_number: str,
    reviewer_number_map: dict[str, int] | None,
) -> str:
    """CLI引数の番号指定 ("3") を実名 ("basho") に解決する。

    - name_or_number が数字文字列の場合:
      - reviewer_number_map が None → SystemExit（番号指定は利用不可）
      - reviewer_number_map に該当番号がない → SystemExit（無効な番号）
      - 該当番号がある → 実名を返す
    - name_or_number が数字でない場合: そのまま返す（実名フォールバック）。
    """
    if not name_or_number.isdigit():
        return name_or_number
    if reviewer_number_map is None:
        raise SystemExit(
            "Reviewer number is only available after batch start"
        )
    number = int(name_or_number)
    reverse_map = {v: k for k, v in reviewer_number_map.items()}
    if number not in reverse_map:
        raise SystemExit(
            f"Reviewer {number} not found in current batch. "
            f"Check GitLab issue comments for valid reviewer numbers"
        )
    return reverse_map[number]


def post_gitlab_note(gitlab: str, issue_num: int, body: str) -> bool:
    """glab issue note を投稿。失敗時は2回リトライ（間隔3秒）。"""
    for attempt in range(3):
        try:
            result = subprocess.run(
                [GLAB_BIN, "issue", "note", str(issue_num), "-m", body, "-R", gitlab],
                capture_output=True, text=True, timeout=GLAB_TIMEOUT,
            )
            if result.returncode == 0:
                return True
            logger.warning("glab note failed (attempt %d/3): %s", attempt + 1, result.stderr.strip())
        except Exception as e:
            logger.warning("glab note error (attempt %d/3): %s", attempt + 1, e)
        if attempt < 2:
            time.sleep(3)
    logger.error("GitLab note failed after 3 attempts")
    return False


def _npass_mapping_path(project: str) -> Path:
    """NPASS レビューファイルマッピングのパスを返す。"""
    sanitized = re.sub(r'[/\\\s]', '-', project)
    return REVIEW_FILE_DIR / f"{sanitized}--npass-files.json"


def _save_npass_review_file_path(project: str, reviewer: str, file_path: Path) -> None:
    """NPASS 用にレビューファイルパスを保存。"""
    mapping_path = _npass_mapping_path(project)
    try:
        os.makedirs(REVIEW_FILE_DIR, exist_ok=True)
        mapping: dict[str, str] = {}
        if mapping_path.exists():
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        mapping[reviewer] = str(file_path)
        # アトミック書き込み（tempfile + rename）で破損リスクを回避
        import tempfile
        content = json.dumps(mapping, ensure_ascii=False)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(REVIEW_FILE_DIR), suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp_f:
                tmp_f.write(content)
            os.replace(tmp_path, str(mapping_path))
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError as e:
        logger.warning("Failed to save NPASS review file path: %s", e)


def _load_npass_review_file_path(project: str, reviewer: str) -> str | None:
    """NPASS 用にレビューファイルパスを読み込み。"""
    mapping_path = _npass_mapping_path(project)
    try:
        if not mapping_path.exists():
            return None
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        return mapping.get(reviewer)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to load NPASS review file path: %s", e)
        return None


def cleanup_npass_files(project: str) -> None:
    """NPASS 用レビューファイルとマッピングメタ情報を削除。

    メタファイル（マッピング）に加え、参照先のレビュー本体ファイルも削除する。
    パイプライン終了時のゴミファイル残留を防ぐため。
    """
    mapping_path = _npass_mapping_path(project)
    try:
        if mapping_path.exists():
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
            for path_str in mapping.values():
                try:
                    Path(path_str).unlink(missing_ok=True)
                except OSError:
                    pass
            mapping_path.unlink(missing_ok=True)
    except (OSError, json.JSONDecodeError):
        pass


def _trigger_blocked(project: str, reason: str) -> None:
    """パイプラインをBLOCKED状態に遷移させる。

    notify.pyからのBLOCKED遷移は異常系のみ。
    gokrax CLI経由で遷移する。
    --force を使う理由: notify_reviewers() は DESIGN_REVIEW/CODE_REVIEW 状態から
    呼ばれるが、BLOCKED への遷移は正規遷移表に含まれるため --force は本来不要。
    ただし、watchdog の状態遷移タイミングと競合した場合（例: REVISE への遷移中）の
    安全策として --force を付与する。
    reason はログにのみ記録する（pipeline JSON への永続化は行わない）。
    BLOCKED 遷移の reason 永続化が必要になった場合は別 Issue で対応する。
    """
    try:
        result = subprocess.run(
            [str(config.GOKRAX_CLI), "transition",
             "--project", project, "--to", "BLOCKED", "--force"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode == 0:
            logger.error("Pipeline %s → BLOCKED: %s", project, reason)
        else:
            logger.error(
                "Failed to transition %s → BLOCKED (rc=%d): %s | reason: %s",
                project, result.returncode, result.stderr.strip()[:200], reason,
            )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.error("Failed to trigger BLOCKED for %s: %s | reason: %s", project, e, reason)


def _ping_agent_openclaw(agent_id: str, timeout: int = 20) -> bool:
    """OpenClaw-specific ping via ``openclaw agent`` CLI."""
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


def ping_agent(agent_id: str, timeout: int = 20) -> bool:
    """Ping agent, dispatching to the selected backend.

    Returns True if agent is alive, False otherwise.
    """
    from engine.backend import ping as _dispatch_ping
    return _dispatch_ping(agent_id, timeout)


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
            cut = limit  # force break if no newline found
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
            cmd = ["git", "-C", repo_path, "diff", "-W", f"{base_commit}..{commit}"]
        else:
            cmd = ["git", "-C", repo_path, "show", "-W", commit]
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
            return []  # continue if verification is not possible

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
        return []  # continue if verification is not possible

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
            pass  # skip if verification is not possible
        predecessor = commit

    return warnings


def notify_implementer(agent_id: str, message: str, project: str = "", phase: str = ""):
    """実装担当にメッセージを送信する。

    agent_id に紐付けられたスキルブロック（config.AGENT_SKILLS）がある場合、
    message の先頭に自動付与する。
    """
    if agent_id not in AGENTS:
        logger.error("Unknown agent: %s", agent_id)
        return
    skill_block = load_skills(agent_id, project, phase)
    if skill_block:
        message = f"{skill_block}\n\n{message}"
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
        f"[Dispute]\n"
        f"{project} #{issue_num} : the implementer has filed a dispute against your P0/P1 verdict.\n\n"
        f"Reason: {reason}\n\n"
        f"Please re-evaluate and report your verdict using one of the following commands:\n\n"
        f"# To change your verdict:\n"
        f"{GOKRAX_CLI} review --pj {project} --issue {issue_num} "
        f"--reviewer {reviewer} --verdict APPROVE --force\n"
        f"{GOKRAX_CLI} review --pj {project} --issue {issue_num} "
        f"--reviewer {reviewer} --verdict P2 --summary \"reason\" --force\n"
        f"{GOKRAX_CLI} review --pj {project} --issue {issue_num} "
        f"--reviewer {reviewer} --verdict P1 --summary \"reason\" --force\n\n"
        f"# To maintain your current verdict:\n"
        f"{GOKRAX_CLI} review --pj {project} --issue {issue_num} "
        f"--reviewer {reviewer} --verdict P0 --summary \"reason for maintaining\" --force\n"
        f"{GOKRAX_CLI} review --pj {project} --issue {issue_num} "
        f"--reviewer {reviewer} --verdict P1 --summary \"reason for maintaining\" --force\n\n"
        f"Note: --force is required (needed to overwrite existing review).\n"
        f"Note: even when maintaining, you must explicitly report via command.\n"
        f"Note: use the --verdict that matches your current verdict."
    )
    return send_to_agent(reviewer, msg)


def notify_reviewers(project: str, state: str, batch: list, gitlab: str,
                     repo_path: str = "", review_mode: str = "standard",
                     prev_reviews: dict = None, excluded: list[str] = None,
                     base_commit: str | None = None,
                     comment: str = "",
                     round_num: int | None = None,
                     already_reset: bool = False) -> list[str]:
    """各レビュアーに個別のメッセージを送信。

    review_mode が "skip" の場合は通知をスキップ（自動承認用）。
    バッチ開始時に全レビュアーへ /new を送信してセッションリセット。
    excluded に含まれるレビュアーには通知しない。

    already_reset: True の場合、_reset_reviewers() が既に実行済み（/new 送信 + 待機完了）。

    Returns: 送信失敗したレビュアーのリスト（重複なし、空なら全員成功）。
    """
    if prev_reviews is None:
        prev_reviews = {}
    if excluded is None:
        excluded = []

    failed_set: set[str] = set()

    # review_mode 検証
    if review_mode not in REVIEW_MODES:
        logger.error("Invalid review_mode: %s, defaulting to 'standard'", review_mode)
        review_mode = "standard"

    mode_config = REVIEW_MODES[review_mode]
    reviewers = mode_config["members"]

    # "skip" モード: 通知なし（watchdog が自動承認を処理）
    if review_mode == "skip":
        logger.info("[review_mode=skip] Skipping reviewer notifications for %s", project)
        return []

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
            continue

        if state in ("DESIGN_REVIEW_NPASS", "CODE_REVIEW_NPASS"):
            msg = _build_npass_review_message(
                project, state, batch, reviewer=r,
                round_num=round_num, comment=comment,
                gitlab=gitlab,
            )
        else:
            msg = format_review_request(project, state, batch, gitlab, reviewer=r,
                                        repo_path=repo_path, prev_reviews=prev_reviews,
                                        base_commit=base_commit, comment=comment,
                                        round_num=round_num)
        if not msg:
            logger.info("No pending issues for %s — skipping review request", r)
            continue

        # 強制外部化: CODE_REVIEW で n_pass > 1 のレビュアー → NPASS 用にファイル参照を保存
        force_externalize = False
        if state == "CODE_REVIEW":
            n_pass = mode_config.get("n_pass", {}).get(r, 1)
            if n_pass > 1:
                force_externalize = True

        # サイズ判定: CLI引数として渡す最終形態（JSON）のバイト数で MAX_CLI_ARG_BYTES と比較
        params_json_size = len(json.dumps({
            "sessionKey": f"agent:{r}:main",
            "message": msg,
            "idempotencyKey": "00000000-0000-0000-0000-000000000000",
        }).encode("utf-8"))
        if force_externalize or params_json_size >= config.MAX_CLI_ARG_BYTES:
            logger.info("Review message for %s is %d bytes (json), externalizing to file%s",
                        r, params_json_size, " (forced for n_pass)" if force_externalize else "")
            file_path = _write_review_file(project, r, msg)
            if file_path is None:
                logger.error("Failed to write review file for %s, skipping", r)
                failed_set.add(r)
                continue
            # NPASS 用にファイルパスを保存
            if force_externalize:
                _save_npass_review_file_path(project, r, file_path)
            is_code = "CODE" in state
            is_npass = state in ("DESIGN_REVIEW_NPASS", "CODE_REVIEW_NPASS")
            short_msg = _build_file_review_message(project, is_code, r, file_path, batch, round_num, skip_skills=is_npass)
            if not send_to_agent(r, short_msg):
                logger.warning("Failed to send review request to %s", r)
                failed_set.add(r)
                continue
        else:
            if not send_to_agent(r, msg):
                logger.warning("Failed to send review request to %s", r)
                failed_set.add(r)
                continue

        # メトリクス記録（Issue #81）
        from pipeline_io import append_metric
        phase_key = "code" if "CODE" in state else "design"
        review_key = "code_reviews" if "CODE" in state else "design_reviews"
        for i in batch:
            existing = i.get(review_key, {}).get(r, {})
            # NPASS: pass < target_pass なら APPROVE 済みでもメトリクス記録（次パスの通知対象）
            if existing.get("verdict", "").upper() == "APPROVE":
                if existing.get("pass", 1) >= existing.get("target_pass", 1):
                    continue
            append_metric("review_request", pj=project, issue=i["issue"],
                          phase=phase_key, reviewer=r)

    # 全員失敗時のみ BLOCKED
    effective_set = {r for r in reviewers if r not in excluded and r in AGENTS}
    if effective_set and failed_set >= effective_set:
        _trigger_blocked(project, f"Failed to send to all reviewers: {sorted(failed_set)}")

    return sorted(failed_set)


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
                          comment: str = "",
                          round_num: int | None = None) -> str:
    """レビュー依頼メッセージを生成（データ埋め込み + 20000文字制限）。"""
    if prev_reviews is None:
        prev_reviews = {}

    is_code = "CODE" in state
    phase = "code" if is_code else "design"
    sections = []

    skill_phase = "code" if is_code else "design"
    skill_block = load_skills(reviewer, project, skill_phase)

    diff_commits: set[str] = set()

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
                section_parts[0] = f"### #{num}: {title}(re-review — {prev_verdict} addressed)\n"
                if prev_summary:
                    quoted = "\n".join(f"> {line}" for line in prev_summary.split("\n"))
                    section_parts.insert(1, f"**Previous {prev_verdict} findings (yours):**\n{quoted}\n\n")

        # dispute 理由の埋め込み (Issue #108)
        dispute_phase = "code" if is_code else "design"
        for disp in i.get("disputes", []):
            if (disp.get("reviewer") == reviewer
                    and disp.get("status") == "pending"
                    and disp.get("phase") == dispute_phase):
                dispute_reason = disp.get("reason", "").strip()
                if dispute_reason:
                    quoted_reason = "\n".join(f"> {line}" for line in dispute_reason.splitlines())
                    section_parts.append(f"**Dispute from implementer (against your {disp.get('filed_verdict', 'P0')}):**\n{quoted_reason}\n\n")
                break  # at most one pending dispute per reviewer (guaranteed by cmd_dispute)

        # Issue本文を取得して埋め込み
        issue_body = fetch_issue_body(num, gitlab)
        if issue_body:
            section_parts.append(f"**Issue body:**\n```\n{issue_body}\n```\n")
        else:
            # 取得失敗時: fallbackコマンド
            section_parts.append(f"Issue details: `{GLAB_BIN} issue show {num} -R {gitlab}`\n")

        # コードレビュー: diff参照を記録（埋め込みはループ外で一括）
        if is_code and commit and repo_path:
            diff_commits.add(commit)

        section_text = "".join(section_parts)

        sections.append(section_text)

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
        round_arg = f" --round {round_num}" if round_num is not None else ""
        pending_cmds.append(
            f"{GOKRAX_CLI} review --project {project} --issue {num}"
            f"{round_arg}"
            f" --reviewer {reviewer} --verdict <APPROVE|P0|P1|P2> "
            f"--summary $'review body\nline 2\nline 3..'"
        )

    todo_header = (
        f"[Task: {len(pending_issues)} items — do not stop until all are completed]\n"
        + "\n".join(pending_issues)
        + "\n\n⚠️ Anonymous review: do not include your name or agent name in --summary."
    )

    body = "\n\n".join(sections)

    # コードレビュー: diffをcommit単位で重複排除して末尾に一括添付
    if is_code and repo_path and diff_commits:
        for commit_hash in sorted(diff_commits):
            diff = _fetch_commit_diff(commit_hash, repo_path)
            if diff:
                orig_len = len(diff)
                if orig_len > MAX_DIFF_CHARS:
                    diff = diff[:MAX_DIFF_CHARS] + f"\n\n... (safety truncated: {orig_len} chars exceeds {MAX_DIFF_CHARS} hard limit)"
                body += f"\n\n---\n**Changes (commit {commit_hash[:7]}):**\n```diff\n{diff}\n```\n"
            else:
                body += (
                    f"\n\n---\nVerify changes (commit {commit_hash[:7]}):\n"
                    f"  `git -C {repo_path} show --stat {commit_hash}`\n"
                    f"  `git -C {repo_path} show {commit_hash}`\n"
                )

    from messages import render
    if is_code:
        guidance = render("dev.code_review", "guidance_code")
    else:
        guidance = render("dev.design_review", "guidance_design")

    # --- 完了コマンド一覧（末尾） ---
    cmds_block = "\n".join(pending_cmds)
    completion = (
        f"\n\n[Completion commands — execute for all Issues]\n"
        f"```\n{cmds_block}\n```\n"
        f"⚠️ Task is incomplete until review commands are executed for all Issues. Do not stop midway."
    )

    from config import OWNER_NAME
    comment_line = f"\n{OWNER_NAME}'s request: {comment}" if comment else ""
    phase_note = "" if is_code else render("dev.design_review", "phase_note")
    review_module = "dev.code_review" if is_code else "dev.design_review"
    final_message = render(review_module, "review_request",
        project=project, todo_header=todo_header, guidance=guidance,
        body=body, completion=completion, comment_line=comment_line,
        phase_note=phase_note,
    )
    # skill_block が非空なら先頭に挿入
    if skill_block:
        final_message = f"{skill_block}\n\n{final_message}"
    return final_message


# ---------------------------------------------------------------------------
# Spec mode notification formatters (§11)
# ---------------------------------------------------------------------------

def spec_notify_review_start(project: str, rev: str | int, reviewer_count: int) -> str:
    """→ SPEC_REVIEW"""
    from messages import render
    return render("spec.review", "notify_start", project=project, rev=rev, reviewer_count=reviewer_count)


def spec_notify_review_complete(
    project: str, rev: str | int,
    critical: int, major: int, minor: int, suggestion: int,
) -> str:
    """→ SPEC_REVISE"""
    from messages import render
    return render("spec.review", "notify_complete", project=project, rev=rev, critical=critical, major=major, minor=minor, suggestion=suggestion)


def spec_notify_approved(project: str, rev: str | int) -> str:
    """→ SPEC_APPROVED（通常、オーナー確認待ち）"""
    from messages import render
    return render("spec.approved", "notify_approved", project=project, rev=rev)


def spec_notify_approved_auto(project: str, rev: str | int) -> str:
    """→ SPEC_APPROVED（auto_continue: 自動進行）"""
    from messages import render
    return render("spec.approved", "notify_approved_auto", project=project, rev=rev)


def spec_notify_approved_forced(project: str, rev: str | int, remaining_p1_plus: int) -> str:
    """→ SPEC_APPROVED（強制承認 via `spec approve --force`）"""
    from messages import render
    return render("spec.approved", "notify_approved_forced", project=project, rev=rev, remaining_p1_plus=remaining_p1_plus)


def spec_notify_stalled(project: str, rev: str | int, remaining_p1_plus: int) -> str:
    """→ SPEC_STALLED"""
    from messages import render
    return render("spec.stalled", "notify_stalled", project=project, rev=rev, remaining_p1_plus=remaining_p1_plus)


def spec_notify_review_failed(project: str, rev: str | int) -> str:
    """→ SPEC_REVIEW_FAILED"""
    from messages import render
    return render("spec.review", "notify_failed", project=project, rev=rev)


def spec_notify_paused(project: str, reason: str) -> str:
    """→ SPEC_PAUSED"""
    from messages import render
    return render("spec.paused", "notify_paused", project=project, reason=reason)


def spec_notify_revise_done(project: str, rev: str | int, commit: str) -> str:
    """REVISE完了（commit hashあり）。commit は先頭7文字に短縮（§11補足）。"""
    from messages import render
    return render("spec.revise", "notify_done", project=project, rev=rev, commit=commit)


def spec_notify_revise_commit_failed(project: str, rev: str | int) -> str:
    """REVISE完了（git commit失敗）"""
    from messages import render
    return render("spec.revise", "notify_commit_failed", project=project, rev=rev)


def spec_notify_revise_no_changes(project: str, rev: str | int) -> str:
    """REVISE完了（差分0）→ SPEC_PAUSED"""
    from messages import render
    return render("spec.revise", "notify_no_changes", project=project, rev=rev)


def spec_notify_issue_plan_done(project: str, issue_count: int) -> str:
    """→ ISSUE_PLAN完了"""
    from messages import render
    return render("spec.issue_plan", "notify_done", project=project, issue_count=issue_count)


def spec_notify_queue_plan_done(project: str, batch_count: int) -> str:
    """→ QUEUE_PLAN完了"""
    from messages import render
    return render("spec.queue_plan", "notify_done", project=project, batch_count=batch_count)


def spec_notify_done(project: str) -> str:
    """→ SPEC_DONE"""
    from messages import render
    return render("spec.done", "notify_done", project=project)


def spec_notify_failure(project: str, kind: str, detail: str = "") -> str:
    """失敗系通知（汎用）。kind: "YAMLパース失敗", "送信失敗", "git push失敗", "Issue起票失敗" 等。"""
    from messages import render
    return render("spec.paused", "notify_failure", project=project, kind=kind, detail=detail)


def spec_notify_self_review_failed(project: str, failed_count: int) -> str:
    """セルフレビュー差し戻し通知。"""
    from messages import render
    return render("spec.revise", "notify_self_review_failed", project=project, failed_count=failed_count)
