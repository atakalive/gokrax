"""issue_ops — gokrax issue-* サブコマンド実装

mode 中立 (dev/spec どちらでも使用可能) のため commands/ 直下に置く。
"""

import argparse
import logging
import sys
from pathlib import Path

from config import GITLAB_NAMESPACE, MAX_CLI_ARG_BYTES
from engine.glab import run_glab
from pipeline_io import get_path, load_pipeline

logger = logging.getLogger(__name__)


def cmd_issue_update(args: argparse.Namespace) -> None:
    """`gokrax issue-update` — body-file 経由で Issue 本文を更新する。"""
    project = args.project
    issue_num = args.issue
    body_file = args.body_file
    title = getattr(args, "title", None)

    path = get_path(project)
    data = load_pipeline(path)
    gitlab = data.get("gitlab") or f"{GITLAB_NAMESPACE}/{project}"

    bf = Path(body_file).resolve()
    if not bf.is_file():
        print(f"Error: body-file not found: {bf}", file=sys.stderr)
        sys.exit(1)

    raw = bf.read_text(encoding="utf-8")
    body_bytes = len(raw.encode("utf-8"))
    if body_bytes > MAX_CLI_ARG_BYTES:
        print(
            f"Error: body-file exceeds MAX_CLI_ARG_BYTES ({MAX_CLI_ARG_BYTES} bytes): {body_bytes}",
            file=sys.stderr,
        )
        sys.exit(1)
    if not raw.strip():
        print(f"Error: body-file is empty: {bf}", file=sys.stderr)
        sys.exit(1)

    body = raw

    argv = ["issue", "update", str(issue_num), "-R", gitlab, "-d", body]
    if title:
        argv += ["-t", title]

    try:
        result = run_glab(argv)
    except (PermissionError, OSError) as e:
        print(f"Error: failed to invoke glab: {e}", file=sys.stderr)
        sys.exit(1)

    if result.ok:
        print(f"{project}: issue #{issue_num} updated")
        return

    if isinstance(result.error, FileNotFoundError):
        print("Error: glab binary not found", file=sys.stderr)
        sys.exit(1)

    stderr = (result.stderr or "").strip() or str(result.error or "unknown error")
    logger.warning("glab issue update failed (after retries): %s", stderr)
    print(stderr, file=sys.stderr)
    sys.exit(1)
