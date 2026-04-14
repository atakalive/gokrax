# DOMAIN_RISK.md (gokrax)

Classification granularity: responsibility + file/function path. When a single file spans multiple risk levels, list each responsibility separately.

## High Risk Changes

Bugs directly cause pipeline state corruption, invalid transitions, or data loss.

- pipeline.json read/write and file locking (`pipeline_io.py`)
- State transition logic (`engine/fsm.py`, `engine/fsm_spec.py`, `watchdog.py`, `config/states.py`)
- CC CLI launch and parameter assembly (`engine/cc.py`: `_start_cc`, `_start_cc_test_fix`)
- Review aggregation, verdict calculation, and review clearing (`engine/reviewer.py`)
- CLI commands that update state/history/spec_config (`commands/dev/`: `cmd_transition`, `cmd_review`, `cmd_commit`, `cmd_start`, etc.; `commands/spec.py`: `cmd_spec_start`, `cmd_spec_approve`, etc.)

## Low Risk Changes

Bug impact is limited, or failures are detectable and manually recoverable.

- Queue file writes (`task_queue.py`: pop/restore/append/replace/delete)
- Queue parsing and token normalization (`task_queue.py`: `parse_queue_line`)
- glab API calls — issue close, title update, comment posting (`commands/dev/`: `_update_issue_title_with_assessment`, etc.; `engine/cc.py`: `_auto_push_and_close`)
- git push / merge (`engine/cc.py`: `_auto_push_and_close`)
- Prompt template structural changes — adding output keys, etc. (`messages/`)
- Test infrastructure (`tests/conftest.py`)
- Settings update script (`update_settings.py`)
- CLI entrypoint and argparse definitions (`gokrax.py`)
- Notification logic — Discord/agent notifications, GitLab note posting (`notify.py`)
- Spec review and revision processing (`spec_review.py`, `spec_revise.py`, `spec_issue.py`)
- Watchdog helpers — process status checks, etc. (`engine/shared.py`)

## No Risk

Changes that do not affect runtime behavior.

- Discord notification message formatting (`messages/` — wording only)
- Prompt template wording-only changes (`messages/` — no output key changes)
- Documentation (`README.md`, `CLI.md`, `docs/`, etc.)
- Test code (`tests/` excluding `tests/conftest.py`)
- Scripts (`scripts/`)
