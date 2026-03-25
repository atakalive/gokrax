"""engine/cc — CC CLI 自動化ロジック。

watchdog.py から切り出し (#129)。
"""

import os
import signal
from datetime import datetime as _datetime
from pathlib import Path

from config import CC_MODEL_PLAN, CC_MODEL_IMPL, LOCAL_TZ, GOKRAX_CLI
from pipeline_io import load_pipeline, update_pipeline, now_iso
from engine.shared import log
from messages import render

# Issue #92: pytest ベースライン定数
PYTEST_BASELINE_TIMEOUT_SEC = 300   # 5分
MAX_BASELINE_OUTPUT_CHARS   = 50_000
MAX_BASELINE_EMBED_CHARS    = 30_000
KILL_GRACE_SEC: float       = 2.0


def _start_cc(project: str, batch: list, gitlab: str, repo_path: str, pipeline_path: Path) -> None:
    """CC を非同期起動し、PID を記録。"""
    import subprocess as _sub
    import uuid as _uuid
    import os
    import tempfile
    from notify import fetch_issue_body

    # CC モデル指定を pipeline JSON から読み取る (Issue #45)
    data = load_pipeline(pipeline_path)
    skip_plan = data.get("skip_cc_plan", False)
    log(f"[{project}] _start_cc: skip_cc_plan={skip_plan}")

    # base_commit フォールバック: DESIGN_PLAN 遷移で記録されていない場合のみ
    if not data.get("base_commit") and repo_path:
        try:
            result = _sub.run(
                ["git", "-C", repo_path, "log", "--format=%H", "-1"],
                capture_output=True, text=True, timeout=10, check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                base = result.stdout.strip()
                def _set_base(d):
                    if not d.get("base_commit"):
                        d["base_commit"] = base
                update_pipeline(pipeline_path, _set_base)
                log(f"[{project}] base_commit fallback recorded: {base[:7]}")
        except Exception as e:
            log(f"[{project}] WARNING: failed to record base_commit: {e}")

    plan_model = data.get("cc_plan_model") or CC_MODEL_PLAN
    impl_model = data.get("cc_impl_model") or CC_MODEL_IMPL
    q_tag = "[Queue]" if data.get("queue_mode") else ""

    # keep_ctx_batch: 前バッチの cc_session_id を再利用 (Issue #58)
    prev_session = data.get("cc_session_id") if data.get("keep_ctx_batch") else None
    session_id = prev_session or str(_uuid.uuid4())

    # Issue本文を収集
    issue_nums: list[int] = []
    issue_texts: list[str] = []
    for item in batch:
        if item.get("commit"):
            continue
        num = item["issue"]
        issue_nums.append(num)
        body = fetch_issue_body(num, gitlab) or f"(Issue #{num} の本文取得失敗)"
        issue_texts.append(f"### Issue #{num}: {item.get('title', '')}\n{body}")

    if not issue_nums:
        return

    issues_block = "\n\n".join(issue_texts)
    closes = " ".join(f"Closes #{n}" for n in issue_nums)
    issue_args = " ".join(str(n) for n in issue_nums)

    comment = data.get("comment", "")
    from config import OWNER_NAME as _owner
    comment_line = f"{_owner}からの要望: {comment}\n\n" if comment else ""
    plan_prompt = render("dev.implementation", "cc_plan",
        issues_block=issues_block, closes=closes, comment_line=comment_line,
    )
    # Issue #92: テストベースライン埋め込み
    test_baseline_section = ""
    baseline = data.get("test_baseline")
    if baseline and repo_path:
        import subprocess as _sub_bl
        try:
            current_head = _sub_bl.run(
                ["git", "-C", repo_path, "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=10, check=True,
            ).stdout.strip()
            if current_head == baseline["commit"]:
                bl_exit   = baseline.get("exit_code", -1)
                bl_output = baseline.get("output", "")
                if len(bl_output) > MAX_BASELINE_EMBED_CHARS:
                    bl_output = "(truncated)\n..." + bl_output[-(MAX_BASELINE_EMBED_CHARS - 20):]
                if bl_exit == 0:
                    test_baseline_section = render("dev.implementation", "test_baseline_pass",
                        bl_output=bl_output,
                    )
                else:
                    test_baseline_section = render("dev.implementation", "test_baseline_fail",
                        bl_exit=bl_exit, bl_output=bl_output,
                    )
                log(f"[{project}] test baseline embedded (exit_code={bl_exit})")
            else:
                log(f"[{project}] test baseline skipped: HEAD mismatch ({current_head[:8]} != {baseline['commit'][:8]})")
        except Exception as e:
            log(f"[{project}] WARNING: test baseline embed failed: {e}")

    if skip_plan:
        scope_warning = render("dev.implementation", "scope_warning_skip_plan")
        impl_prompt = render("dev.implementation", "cc_impl_skip_plan",
            issues_block=issues_block, closes=closes, comment_line=comment_line,
            scope_warning=scope_warning, test_baseline_section=test_baseline_section,
        )
    else:
        scope_warning = render("dev.implementation", "scope_warning_normal")
        impl_prompt = render("dev.implementation", "cc_impl_resume",
            closes=closes, scope_warning=scope_warning,
            test_baseline_section=test_baseline_section,
        )

    # mkstemp で安全に一時ファイル作成
    plan_path: str | None = None
    impl_path: str | None = None
    script_path: str | None = None

    try:
        if skip_plan:
            fd_impl, impl_path = tempfile.mkstemp(suffix=".txt", prefix="gokrax-impl-")
            fd_script, script_path = tempfile.mkstemp(suffix=".sh", prefix="gokrax-cc-")
        else:
            fd_plan, plan_path = tempfile.mkstemp(suffix=".txt", prefix="gokrax-plan-")
            fd_impl, impl_path = tempfile.mkstemp(suffix=".txt", prefix="gokrax-impl-")
            fd_script, script_path = tempfile.mkstemp(suffix=".sh", prefix="gokrax-cc-")

        if plan_path is not None:
            os.write(fd_plan, plan_prompt.encode())
            os.close(fd_plan)
        os.write(fd_impl, impl_prompt.encode())
        os.close(fd_impl)

        # --- _notify 用（7箇所） ---
        msg_plan_start = render("dev.implementation", "notify_cc_plan_start",
            project=project, plan_model=plan_model, q_tag=q_tag)
        msg_plan_done = render("dev.implementation", "notify_cc_plan_done",
            project=project, q_tag=q_tag)
        msg_impl_start = render("dev.implementation", "notify_cc_impl_start",
            project=project, impl_model=impl_model, q_tag=q_tag)
        msg_impl_done = render("dev.implementation", "notify_cc_impl_done",
            project=project, q_tag=q_tag)
        msg_impl_start_skip = render("dev.implementation", "notify_cc_impl_start_skip_plan",
            project=project, impl_model=impl_model, q_tag=q_tag)
        msg_no_commit_retry = render("dev.implementation", "notify_cc_no_commit_retry",
            project=project, retry="$RETRY/2", q_tag=q_tag)
        msg_no_commit_blocked = render("dev.implementation", "notify_cc_no_commit_blocked",
            project=project, q_tag=q_tag)

        # --- echo 用（1箇所: CCへのリトライ指示プロンプト） ---
        msg_commit_retry = render("dev.implementation", "cc_commit_retry", closes=closes)
        # cc_commit_retry の戻り値には " が含まれる（git commit -m "feat(...)"）
        # bash の echo "..." 内に埋め込むため、" をエスケープする
        msg_commit_retry_escaped = msg_commit_retry.replace('"', '\\"')

        # コミット検証+リトライブロック（skip_plan/通常 共通）
        commit_verify_block = f'''
# コミット検証: CC が実際に新しいコミットを作ったか確認し、なければリトライ
HASH=$(git rev-parse --short HEAD)
RETRY=0
while [ "$HASH" = "$BEFORE_HASH" ] && [ "$RETRY" -lt 2 ]; do
    RETRY=$((RETRY + 1))
    _notify "{msg_no_commit_retry}"
    echo "{msg_commit_retry_escaped}" | \\
    claude -p --model "{impl_model}" --resume "{session_id}" \\
      --permission-mode bypassPermissions --output-format json
    HASH=$(git rev-parse --short HEAD)
done

if [ "$HASH" = "$BEFORE_HASH" ]; then
    _notify "{msg_no_commit_blocked}"
    "{GOKRAX_CLI}" transition --project "{project}" --to BLOCKED --force
    exit 1
fi

# gokrax commit
"{GOKRAX_CLI}" commit --project "{project}" --issue {issue_args} --hash "$HASH" --session-id "{session_id}"
'''

        if skip_plan:
            script_content = f'''#!/bin/bash
set -e
cleanup() {{ rm -f "{script_path}" "{impl_path}"; }}
trap cleanup EXIT

cd "{repo_path}"

_notify() {{ local ts=$(date +"%m/%d %H:%M"); python3 -c "import sys; sys.path.insert(0,'{Path(GOKRAX_CLI).resolve().parent}'); from notify import notify_discord; notify_discord(sys.argv[1])" "$1 ($ts)" 2>/dev/null || true; }}

BEFORE_HASH=$(git rev-parse --short HEAD)

# Plan フェーズなし — 直接 Impl
_notify "{msg_impl_start_skip}"
claude -p --model "{impl_model}" {"--resume" if prev_session else "--session-id"} "{session_id}" \
  --permission-mode bypassPermissions --output-format json < "{impl_path}"
_notify "{msg_impl_done}"
{commit_verify_block}'''
        else:
            script_content = f'''#!/bin/bash
set -e
cleanup() {{ rm -f "{script_path}" "{plan_path}" "{impl_path}"; }}
trap cleanup EXIT

cd "{repo_path}"

_notify() {{ local ts=$(date +"%m/%d %H:%M"); python3 -c "import sys; sys.path.insert(0,'{Path(GOKRAX_CLI).resolve().parent}'); from notify import notify_discord; notify_discord(sys.argv[1])" "$1 ($ts)" 2>/dev/null || true; }}

BEFORE_HASH=$(git rev-parse --short HEAD)

# Phase 1: Plan
_notify "{msg_plan_start}"
claude -p --model "{plan_model}" {"--resume" if prev_session else "--session-id"} "{session_id}" \
  --permission-mode plan --output-format json < "{plan_path}"
_notify "{msg_plan_done}"

# Phase 2: Impl
_notify "{msg_impl_start}"
claude -p --model "{impl_model}" --resume "{session_id}" \
  --permission-mode bypassPermissions --output-format json < "{impl_path}"
_notify "{msg_impl_done}"
{commit_verify_block}'''
        os.write(fd_script, script_content.encode())
        os.close(fd_script)
        os.chmod(script_path, 0o700)

        proc = _sub.Popen(
            ["bash", script_path],
            stdout=_sub.DEVNULL,
            stderr=_sub.STDOUT,
            start_new_session=True,
        )

    except Exception:
        for p in filter(None, [plan_path, impl_path, script_path]):
            try:
                os.unlink(p)
            except OSError:
                pass
        raise

    # cc_pid + cc_session_id を記録
    def _save_cc_info(data):
        data["cc_pid"] = proc.pid
        data["cc_session_id"] = session_id
    try:
        update_pipeline(pipeline_path, _save_cc_info)
    except Exception:
        # PID が記録できないと watchdog が kill できなくなるので、
        # 子プロセスを即座に殺して例外を再送出する。
        # 即 SIGKILL の理由: 起動直後で CC は作業未開始、graceful shutdown 不要。
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        raise
    log(f"[{project}] CC started (pid={proc.pid}, session={session_id})")


# ── Issue #92: pytest ベースライン ──────────────────────────────────────────

def _has_pytest(repo_path: str) -> bool:
    """repo に pytest が設定されているかを確認する。"""
    try:
        pyproject = Path(repo_path) / "pyproject.toml"
        if pyproject.exists():
            t = pyproject.read_text(errors="replace")
            if "[tool.pytest" in t or "[pytest]" in t:
                return True
        setup_cfg = Path(repo_path) / "setup.cfg"
        if setup_cfg.exists():
            if "[tool:pytest]" in setup_cfg.read_text(errors="replace"):
                return True
        if (Path(repo_path) / "tests").is_dir():
            return True
    except Exception:
        pass
    return False


def _killpg_graceful(pid: int, grace_sec: float = KILL_GRACE_SEC) -> bool:
    """SIGTERM → ポーリング待機 → SIGKILL のグレースフルキルを行う。

    os.killpg(pid, 0) でプロセスグループ単位の生存を判定する。
    /proc/{pid} はグループリーダしか見ないため使用しない。

    Returns:
        True: SIGKILL まで必要だった
        False: SIGTERM で終了した or 既に死んでいた

    Note: 戻り値はログ用途のみ。この値で処理分岐を行わないこと。
    """
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        return False
    import time
    deadline = time.monotonic() + grace_sec
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except OSError:
            return False
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        return False
    return True


def _kill_pytest_baseline(data: dict, pj: str) -> None:
    """既存の pytest baseline プロセスを停止し、残留ファイルを掃除する。

    start_new_session=True で起動しているため pid == PGID。
    os.killpg でプロセスグループごと停止する（子の pytest も確実に殺す）。
    """
    info = data.pop("_pytest_baseline", None)
    if not info:
        return
    pid = info.get("pid")
    if pid:
        escalated = _killpg_graceful(pid)
        log(f"[{pj}] killed stale pytest baseline (pgid={pid}, escalated={escalated})")
    for key in ("output_path", "exit_code_path"):
        p = info.get(key, "")
        if p:
            try:
                os.unlink(p)
            except OSError:
                pass


def _poll_pytest_baseline(path: Path, pj: str) -> None:
    """バックグラウンド pytest の完了を検出し、test_baseline に書き込む。

    ロック外で呼ぶ。完了していれば update_pipeline で結果を書き込む。
    """
    data = load_pipeline(path)
    info = data.get("_pytest_baseline")
    if not info:
        return
    pid = info.get("pid")
    if not pid:
        return

    exit_code_path = info.get("exit_code_path", "")
    output_path    = info.get("output_path", "")
    finished   = bool(exit_code_path and os.path.exists(exit_code_path))
    proc_alive = Path(f"/proc/{pid}").exists()

    # タイムアウト判定
    timed_out = False
    started_at = info.get("started_at", "")
    if started_at:
        try:
            elapsed = (_datetime.now(LOCAL_TZ) - _datetime.fromisoformat(started_at)).total_seconds()
            if elapsed > PYTEST_BASELINE_TIMEOUT_SEC:
                timed_out = True
        except (ValueError, TypeError):
            pass

    if timed_out and not finished:
        if proc_alive:
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass

        def _save_timeout(d):
            d["test_baseline"] = {
                "commit": info["commit"],
                "summary": "(pytest timed out)",
                "exit_code": -1,
                "output": "",
                "timestamp": now_iso(),
            }
            d.pop("_pytest_baseline", None)

        update_pipeline(path, _save_timeout)
        for p in (output_path, exit_code_path):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass
        log(f"[{pj}] pytest baseline timed out (pid={pid})")
        return

    if not finished:
        if proc_alive:
            return  # まだ実行中
        # 異常終了: exit_code_path なし + proc 消滅 → exit_code=-1 で保存

    # 結果回収
    output    = ""
    exit_code = -1
    try:
        if output_path and os.path.exists(output_path):
            with open(output_path) as f:
                output = f.read()
            os.unlink(output_path)
        if exit_code_path and os.path.exists(exit_code_path):
            with open(exit_code_path) as f:
                exit_code = int(f.read().strip())
            os.unlink(exit_code_path)
    except Exception as e:
        log(f"[{pj}] WARNING: pytest baseline output read failed: {e}")

    if len(output) > MAX_BASELINE_OUTPUT_CHARS:
        output = "(truncated)\n..." + output[-(MAX_BASELINE_OUTPUT_CHARS - 20):]

    lines   = output.strip().splitlines()
    summary = lines[-1] if lines else "(no output)"

    def _save_baseline(d):
        d["test_baseline"] = {
            "commit": info["commit"],
            "summary": summary,
            "exit_code": exit_code,
            "output": output,
            "timestamp": now_iso(),
        }
        d.pop("_pytest_baseline", None)

    update_pipeline(path, _save_baseline)
    log(f"[{pj}] pytest baseline completed: exit_code={exit_code}, summary={summary}")


# ── Issue #87: CODE_TEST ゲート ────────────────────────────────────────────

def _start_code_test(project: str, data: dict, pipeline_path: Path) -> None:
    """CODE_TEST 進入時にテストを非同期実行する。"""
    import subprocess as _sub
    import tempfile

    from config import TEST_CONFIG

    cfg = TEST_CONFIG.get(project)
    if cfg is None:
        raise RuntimeError(f"TEST_CONFIG has no entry for project '{project}'")
    test_command = cfg["test_command"]

    # 残留 baseline pytest を停止
    _kill_pytest_baseline(data, project)

    fd_out, output_path = tempfile.mkstemp(suffix=".txt", prefix="gokrax-codetest-")
    os.close(fd_out)
    exit_code_path = output_path + ".exit"

    fd_sh, script_path = tempfile.mkstemp(suffix=".sh", prefix="gokrax-codetest-")

    repo_path_str = data.get("repo_path", "")
    try:
        head = _sub.run(
            ["git", "-C", repo_path_str, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
    except Exception:
        head = ""

    script_content = (
        f"#!/bin/bash\n"
        f'cd "{repo_path_str}"\n'
        f"{test_command} > {output_path} 2>&1\n"
        f"echo $? > {exit_code_path}\n"
        f"rm -f {script_path}\n"
    )

    try:
        os.write(fd_sh, script_content.encode())
        os.close(fd_sh)
        os.chmod(script_path, 0o700)

        proc = _sub.Popen(
            ["bash", script_path],
            stdout=_sub.DEVNULL,
            stderr=_sub.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        for p in (output_path, exit_code_path, script_path):
            try:
                os.unlink(p)
            except OSError:
                pass
        raise

    def _save_code_test(d: dict) -> None:
        d["_code_test"] = {
            "pid": proc.pid,
            "started_at": now_iso(),
            "output_path": output_path,
            "exit_code_path": exit_code_path,
            "script_path": script_path,
            "commit": head,
        }
        d["test_result"] = None

    update_pipeline(pipeline_path, _save_code_test)
    log(f"[{project}] code test started (pid={proc.pid}, commit={head[:8]})")


def _poll_code_test(path: Path, pj: str) -> None:
    """CODE_TEST のバックグラウンドテスト完了を検出し、結果を書き込む。"""
    from config import TEST_CONFIG
    from pipeline_io import append_metric

    data = load_pipeline(path)
    info = data.get("_code_test")
    if not info:
        return
    pid = info.get("pid")
    if not pid:
        return

    exit_code_path = info.get("exit_code_path", "")
    output_path = info.get("output_path", "")
    finished = bool(exit_code_path and os.path.exists(exit_code_path))
    proc_alive = Path(f"/proc/{pid}").exists()

    # タイムアウト判定
    project = data.get("project", pj)
    test_timeout = TEST_CONFIG.get(project, {}).get("test_timeout", 300)
    timed_out = False
    started_at = info.get("started_at", "")
    elapsed_sec = 0.0
    if started_at:
        try:
            elapsed_sec = (_datetime.now(LOCAL_TZ) - _datetime.fromisoformat(started_at)).total_seconds()
            if elapsed_sec > test_timeout:
                timed_out = True
        except (ValueError, TypeError):
            pass

    if timed_out and not finished:
        if proc_alive:
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass

        def _save_timeout(d: dict) -> None:
            d["test_result"] = "fail"
            d["test_output"] = "(test timed out)"
            d["test_retry_count"] = d.get("test_retry_count", 0) + 1
            d.pop("_code_test", None)

        update_pipeline(path, _save_timeout)
        script_path = info.get("script_path", "")
        for p in (output_path, exit_code_path, script_path):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass
        issue_num = data.get("batch", [{}])[0].get("issue") if data.get("batch") else None
        append_metric("test_run", pj=project,
                       issue=issue_num, result="fail",
                       duration_sec=round(elapsed_sec),
                       retry=data.get("test_retry_count", 0) + 1)
        log(f"[{pj}] code test timed out (pid={pid})")
        return

    if not finished:
        if proc_alive:
            return  # まだ実行中
        # プロセス消滅フォールバック

    # 結果回収
    output = ""
    exit_code = -1
    try:
        if output_path and os.path.exists(output_path):
            with open(output_path) as f:
                output = f.read()
            os.unlink(output_path)
        if exit_code_path and os.path.exists(exit_code_path):
            with open(exit_code_path) as f:
                exit_code = int(f.read().strip())
            os.unlink(exit_code_path)
        script_path_result = info.get("script_path", "")
        if script_path_result and os.path.exists(script_path_result):
            os.unlink(script_path_result)
    except Exception as e:
        log(f"[{pj}] WARNING: code test output read failed: {e}")

    if len(output) > MAX_BASELINE_OUTPUT_CHARS:
        output = "(truncated)\n..." + output[-(MAX_BASELINE_OUTPUT_CHARS - 20):]

    passed = exit_code == 0

    def _save_result(d: dict) -> None:
        d["test_result"] = "pass" if passed else "fail"
        d["test_output"] = output
        if passed:
            d["test_retry_count"] = 0
        else:
            d["test_retry_count"] = d.get("test_retry_count", 0) + 1
        d.pop("_code_test", None)

    update_pipeline(path, _save_result)

    issue_num = data.get("batch", [{}])[0].get("issue") if data.get("batch") else None
    result_str = "pass" if passed else "fail"
    metric_fields: dict = {
        "pj": project, "issue": issue_num,
        "result": result_str, "duration_sec": round(elapsed_sec),
        "retry": data.get("test_retry_count", 0) + (0 if passed else 1),
    }
    if not passed:
        # count failed tests from output
        lines = output.strip().splitlines()
        for line in reversed(lines):
            if "failed" in line:
                import re
                m = re.search(r"(\d+) failed", line)
                if m:
                    metric_fields["failed_tests"] = int(m.group(1))
                break
    append_metric("test_run", **metric_fields)
    log(f"[{pj}] code test completed: exit_code={exit_code}, result={result_str}")


def _kill_code_test(data: dict, pj: str) -> None:
    """残留テストプロセスを停止し、一時ファイルを掃除する。"""
    info = data.pop("_code_test", None)
    if not info:
        return
    pid = info.get("pid")
    if pid:
        escalated = _killpg_graceful(pid)
        log(f"[{pj}] killed stale code test (pgid={pid}, escalated={escalated})")
    for key in ("output_path", "exit_code_path", "script_path"):
        p = info.get(key, "")
        if p:
            try:
                os.unlink(p)
            except OSError:
                pass


def _start_cc_test_fix(project: str, batch: list, data: dict, pipeline_path: Path) -> None:
    """CODE_TEST_FIX 進入時に CC を起動してテスト修正を依頼する。"""
    import subprocess as _sub
    import uuid as _uuid
    import tempfile

    from config import CC_MODEL_IMPL
    from messages import render as _render

    impl_model = data.get("cc_impl_model") or CC_MODEL_IMPL
    session_id = data.get("cc_session_id") or str(_uuid.uuid4())
    test_output = data.get("test_output", "")
    retry_count = data.get("test_retry_count", 0)
    repo_path = data.get("repo_path", "")

    from config import MAX_TEST_RETRY
    prompt = _render("dev.code_test_fix", "cc_test_fix",
        project=project, test_output=test_output,
        retry_count=retry_count, max_retry=MAX_TEST_RETRY,
    )

    fd_prompt, prompt_path = tempfile.mkstemp(suffix=".txt", prefix="gokrax-testfix-")
    fd_script, script_path = tempfile.mkstemp(suffix=".sh", prefix="gokrax-testfix-")

    try:
        os.write(fd_prompt, prompt.encode())
        os.close(fd_prompt)

        script_content = (
            f'#!/bin/bash\n'
            f'set -e\n'
            f'cleanup() {{ rm -f "{script_path}" "{prompt_path}"; }}\n'
            f'trap cleanup EXIT\n'
            f'cd "{repo_path}"\n'
            f'claude -p --model "{impl_model}" --resume "{session_id}" \\\n'
            f'  --permission-mode bypassPermissions --output-format json < "{prompt_path}"\n'
        )

        os.write(fd_script, script_content.encode())
        os.close(fd_script)
        os.chmod(script_path, 0o700)

        proc = _sub.Popen(
            ["bash", script_path],
            stdout=_sub.DEVNULL,
            stderr=_sub.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        for p in (prompt_path, script_path):
            try:
                os.unlink(p)
            except OSError:
                pass
        raise

    def _save_cc_info(d: dict) -> None:
        d["cc_pid"] = proc.pid
        d["cc_session_id"] = session_id

    try:
        update_pipeline(pipeline_path, _save_cc_info)
    except Exception:
        # 即 SIGKILL: 起動直後で CC は作業未開始、graceful shutdown 不要。
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        raise
    log(f"[{project}] CC test fix started (pid={proc.pid}, session={session_id})")


# ────────────────────────────────────────────────────────────────────────────


def _mark_push_failed(gitlab: str, batch: list[dict], project: str) -> None:
    """push 全リトライ失敗時に各 Issue タイトルに [PUSH FAILED] を付与（best effort）。"""
    import json as _json
    import subprocess as _sp
    from config import GLAB_BIN

    for item in batch:
        issue_num = item.get("issue")
        if not issue_num:
            continue
        try:
            view_result = _sp.run(
                [GLAB_BIN, "issue", "view", str(issue_num), "-R", gitlab,
                 "--output", "json"],
                capture_output=True, text=True, timeout=30,
            )
            if view_result.returncode != 0:
                log(f"[{project}] Issue #{issue_num} タイトル取得失敗: {view_result.stderr.strip()}")
                continue
            title = _json.loads(view_result.stdout).get("title", "")
            if title.startswith("[PUSH FAILED]"):
                continue
            upd_result = _sp.run(
                [GLAB_BIN, "issue", "update", str(issue_num), "-R", gitlab,
                 "--title", f"[PUSH FAILED] {title}"],
                capture_output=True, text=True, timeout=30,
            )
            if upd_result.returncode != 0:
                log(f"[{project}] Issue #{issue_num} タイトル更新失敗: {upd_result.stderr.strip()}")
        except Exception as e:
            log(f"[{project}] Issue #{issue_num} タイトル更新エラー: {e}")


def _auto_push_and_close(repo_path: str, gitlab: str, batch: list, project: str) -> None:
    """DONE遷移時に git push + issue close を自動実行。"""
    import subprocess as _sp
    import time
    from config import GLAB_BIN

    MAX_PUSH_RETRIES = 3
    PUSH_RETRY_DELAY = 3

    # git push
    if repo_path:
        push_ok = False
        for attempt in range(1, MAX_PUSH_RETRIES + 1):
            try:
                result = _sp.run(
                    ["git", "-C", repo_path, "push"],
                    capture_output=True, text=True, timeout=60,
                )
                if result.returncode == 0:
                    log(f"[{project}] git push 成功")
                    push_ok = True
                    break
                else:
                    log(f"[{project}] git push 失敗: {result.stderr.strip()}")
            except Exception as e:
                log(f"[{project}] git push エラー: {e}")
            if attempt < MAX_PUSH_RETRIES:
                log(f"[{project}] git push リトライ {attempt}/{MAX_PUSH_RETRIES}")
                time.sleep(PUSH_RETRY_DELAY)
        if not push_ok:
            log(f"[{project}] git push 全リトライ失敗 — issue close をスキップ")
            _mark_push_failed(gitlab, batch, project)
            return

    # issue close
    for item in batch:
        issue_num = item.get("issue")
        if not issue_num:
            continue
        try:
            result = _sp.run(
                [GLAB_BIN, "issue", "close", str(issue_num), "-R", gitlab],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                log(f"[{project}] Issue #{issue_num} closed")
            else:
                log(f"[{project}] Issue #{issue_num} close失敗: {result.stderr.strip()}")
        except Exception as e:
            log(f"[{project}] Issue #{issue_num} closeエラー: {e}")
