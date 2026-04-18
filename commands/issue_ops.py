"""issue_ops — gokrax issue-* サブコマンド実装

mode 中立 (dev/spec どちらでも使用可能) のため commands/ 直下に置く。
"""

import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path

from config import GITLAB_NAMESPACE, MAX_CLI_ARG_BYTES
from notify import GLAB_BIN, GLAB_TIMEOUT
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

    argv = [GLAB_BIN, "issue", "update", str(issue_num), "-R", gitlab, "-d", body]
    if title:
        argv += ["-t", title]

    last_stderr = ""
    for attempt in range(3):
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=GLAB_TIMEOUT,
            )
            if result.returncode == 0:
                print(f"{project}: issue #{issue_num} updated")
                return
            last_stderr = (result.stderr or "").strip()
            logger.warning(
                "glab issue update failed (attempt %d/3): %s", attempt + 1, last_stderr
            )
        except subprocess.TimeoutExpired as e:
            last_stderr = f"timeout after {GLAB_TIMEOUT}s"
            logger.warning(
                "glab issue update timeout (attempt %d/3): %s", attempt + 1, e
            )
        except (FileNotFoundError, PermissionError) as e:
            print(f"Error: failed to invoke {GLAB_BIN}: {e}", file=sys.stderr)
            sys.exit(1)
        except OSError as e:
            last_stderr = f"OSError: {e}"
            logger.warning(
                "glab issue update OSError (attempt %d/3): %s", attempt + 1, e
            )
        if attempt < 2:
            time.sleep(3)

    logger.error("glab issue update failed after 3 attempts: %s", last_stderr)
    print(last_stderr, file=sys.stderr)
    sys.exit(1)
