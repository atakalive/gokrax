"""engine/glab.py — glab サブプロセス実行の汎用リトライヘルパー。

将来他箇所からも再利用できるよう汎用シグネチャで設計する。
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from typing import Literal

import config  # 属性アクセスで参照（from import するとテストの monkeypatch が効かない）


_TRANSIENT_MARKERS = (
    "timeout", "timed out", "connection reset", "connection refused",
    "temporary failure", "could not resolve", "service unavailable",
    "502 ", "503 ", "504 ",
    "rate limit", "too many requests",
)
_PERMANENT_MARKERS = (
    "404", "not found", "unauthorized", "forbidden", "403 ",
    "invalid token", "authentication",
)


@dataclass
class GlabResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int | None
    error: Exception | None


def _classify_error(stderr: str) -> Literal["transient", "permanent", "unknown"]:
    s = stderr.lower()
    if any(m in s for m in _PERMANENT_MARKERS):
        return "permanent"
    if any(m in s for m in _TRANSIENT_MARKERS):
        return "transient"
    return "unknown"


def run_glab(
    argv: list[str],
    *,
    retries: int = 3,
    backoff: float = 1.0,
    timeout: float | None = None,
    input_text: str | None = None,
) -> GlabResult:
    """glab サブプロセスを一時的エラー時にリトライ付きで実行する汎用ラッパー。"""
    actual_timeout = timeout if timeout is not None else config.GLAB_TIMEOUT
    last_stdout = ""
    last_stderr = ""
    last_rc: int | None = None
    last_error: Exception | None = None

    for attempt in range(retries):
        try:
            cp = subprocess.run(
                [config.GLAB_BIN, *argv],
                capture_output=True, text=True,
                timeout=actual_timeout,
                input=input_text,
                check=False,
            )
        except FileNotFoundError as e:
            return GlabResult(ok=False, stdout="", stderr="", returncode=None, error=e)
        except subprocess.TimeoutExpired as e:
            last_error = e
            last_rc = None
            last_stdout = ""
            last_stderr = ""
        else:
            last_stdout = cp.stdout or ""
            last_stderr = cp.stderr or ""
            last_rc = cp.returncode
            last_error = None
            if cp.returncode == 0:
                return GlabResult(ok=True, stdout=last_stdout, stderr=last_stderr,
                                  returncode=0, error=None)
            kind = _classify_error(last_stderr)
            if kind == "permanent":
                return GlabResult(ok=False, stdout=last_stdout, stderr=last_stderr,
                                  returncode=last_rc, error=None)

        if attempt < retries - 1:
            time.sleep(backoff * (2 ** attempt))

    return GlabResult(ok=False, stdout=last_stdout, stderr=last_stderr,
                      returncode=last_rc, error=last_error)


def fetch_issue_state(issue_num: int, gitlab: str) -> str | None:
    """Issue の state を返す。"opened" / "closed" / None。

    None: API 失敗 / JSON パース不能 / state が opened/closed 以外 / Issue 不存在。
    author 検証は行わない（pop 段階では state 判定のみ）。
    """
    result = run_glab(["issue", "show", str(issue_num), "--output", "json", "-R", gitlab], retries=2)
    if not result.ok:
        return None
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    raw = data.get("state")
    return raw if raw in ("opened", "closed") else None
