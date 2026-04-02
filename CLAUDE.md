# CLAUDE.md — gokrax

## Overview

Development pipeline automation tool. Automates the cycle of Issue creation → design review → implementation → code review → merge. CLI + watchdog daemon architecture.

### Architecture

```
# === CLI ===
gokrax.py              # CLI entry point (all command definitions)
commands/dev.py        # Dev mode CLI subcommands
commands/spec.py       # Spec mode CLI subcommands

# === Watchdog daemon ===
watchdog.py            # Main loop (process), Discord handler, queue management
                       # Most logic extracted to engine/

# === engine/ — Core logic extracted from watchdog ===
engine/shared.py       # Shared utilities for watchdog/gokrax
engine/reviewer.py     # Reviewer management (selection, reset, etc.)
engine/cc.py           # CC CLI automation (plan/impl launch, pytest baseline)
engine/fsm.py          # Dev mode state transitions (check_transition, etc.)
engine/fsm_spec.py     # Spec mode state transitions (check_transition_spec, etc.)
engine/backend.py      # Backend abstraction layer (dispatch)
engine/backend_openclaw.py  # OpenClaw backend implementation
engine/backend_pi.py   # PI (Project Interpreter) backend implementation
engine/cleanup.py      # Batch state cleanup
engine/filter.py       # Project/author filtering

# === Foundation ===
config/                # Configuration package
  __init__.py          # Main config (dynamically loads settings.py)
  states.py            # State definitions, transition tables, constants
  paths.py             # File paths and directory constants
notify.py              # Notifications (Discord posts, inter-agent messaging)
pipeline_io.py         # Pipeline JSON read/write (flock exclusive lock)
task_queue.py          # Task queue management
settings.py            # User settings (.gitignore'd)
update_settings.py     # settings.py update utility

# === Spec mode ===
spec_issue.py          # Spec mode: automatic Issue creation
spec_review.py         # Spec mode: spec review
spec_revise.py         # Spec mode: spec revision

# === Externalized messages ===
messages/              # Prompt and notification templates
  __init__.py          # render() entry point
  ja/dev/              # Dev mode Japanese (design_plan, code_review, etc.)
  ja/spec/             # Spec mode Japanese (review, revise, approved, etc.)
  en/dev/              # Dev mode English
  en/spec/             # Spec mode English
messages_custom/       # User-customized prompts (same structure as messages/, overrides)

# === Agents ===
agents/                # Agent profiles (IDENTITY/INSTRUCTION/MEMORY)
  config_pi.json       # PI backend configuration

# === Other ===
reviews/               # Externalized review request files
tests/                 # pytest tests (100+ files)
docs/                  # Documentation (architecture, quick_start, spec, etc.)
```

## Coding Conventions

### Python Style
- **Linter:** ruff
- **Type hints required:** All functions must have parameter and return type hints
  - `list[str]` / `dict[str, Any]` (PEP 585)
  - `X | None` (PEP 604)
- **Tests:** pytest. Place in `tests/` directory
- **Explicit > implicit**
- **Output text in English:** String literals in log, print, raise, Discord notifications, etc. must be in English. Do not hardcode Japanese

### Commit Conventions
- **1 issue = 1 commit** as a rule
- Commit message format: `fix: <description>. Closes #N`
  - type: `fix`, `feat`, `refactor`, `test`, `docs`
- **Always include `Closes #N`.**
- **Always `git add` → `git commit` when implementation is done. Never exit without committing.**
- Push directly to main branch

### Line Endings
- **LF only. Do not use CRLF.**

## Testing

```bash
# Run tests
pytest tests/ -v

# Linter
ruff check *.py engine/ config/ commands/ messages/ tests/
```

### Testing Rules
- This project is developed on Linux. Do not modify tests for Windows compatibility.
- **Do not call `time.sleep()` directly in test code.** `time.sleep` is globally mocked in conftest. Production code sleep calls accumulate during tests and cause timeouts.
- To verify sleep behavior, use `patch("time.sleep") as mock_sleep` and assert call count/arguments.
- **Do not make external calls (Discord, agent send) in tests.** Mocked globally in conftest via `_block_external_calls`. When adding new external call functions, add corresponding mocks to conftest.
- **Patch the binding target after `from ... import`, not the source module.** If `watchdog.py` does `from engine.cc import _start_cc`, then `patch("engine.cc._start_cc")` is ineffective. Use `patch("watchdog._start_cc")` instead. `from X import Y` binds at module load time, so patching the source module does not affect existing bindings.
- **Do not call `_reset_reviewers` / `_reset_short_context_reviewers` in tests.** Mocked in conftest. For direct testing, configure mocks individually as in `test_short_context.py`.

## Design Notes

### Pipeline JSON
- **Do not edit pipeline JSON directly.** Always use `update_pipeline()` in `pipeline_io.py`.
- `update_pipeline()` uses flock(LOCK_EX) blocking exclusive lock. Do not use LOCK_NB.
- Pipeline JSON path: `~/.openclaw/shared/pipelines/<project>.json`

### State Transitions
- Valid states and transitions are defined in `config/states.py` (`VALID_STATES` / `VALID_TRANSITIONS`)
- Transitions are executed via `gokrax transition` CLI command or watchdog's `check_transition()`
- Spec mode is a separate system: `SPEC_STATES` / `SPEC_TRANSITIONS` / `check_transition_spec()`

### Watchdog
- `watchdog-loop.sh` polls every 5 seconds
- Checks each project's state and auto-transitions when conditions are met
- CC launch via `_start_cc()`: generates a bash script and runs it in the background

### Do Not Touch
- Locking mechanism in `pipeline_io.py` (flock LOCK_EX blocking)
- Notification formats that other agents depend on for parsing
- Existing values in `settings.py` (additions OK, changes/deletions require caution)
- `messages_custom/` — User-customized prompts. Do not edit or delete
  - Exception: #280 allows editing `messages_custom/ja/dev/code_revise.py` and `messages_custom/ja/dev/design_revise.py` (glab→gokrax get-comments replacement only)
- Transition tables in `config/states.py` (`VALID_TRANSITIONS`, `SPEC_TRANSITIONS`, `STATE_PHASE_MAP`, `BLOCK_TIMERS`, etc.) must remain as plain strings. Do not convert to `State.XX` references for readability

### Forbidden Commands
The following gokrax CLI commands cause pipeline halt or state corruption. Never run them during development or testing:
- `gokrax reset` — Force-resets all projects to IDLE
- `gokrax transition` — Manually transitions pipeline state
- `gokrax disable` — Stops watchdog
- `gokrax enable` — Starts watchdog
- `gokrax start` / `gokrax qrun` — Starts a new batch

### Known Quirks
- `cmd_transition` in `gokrax.py` (CLI path) and `do_transition` in `watchdog.py` (watchdog path) are separate code paths. Fixing only one may not affect the primary path.
- `cmd_qrun` (CLI) and `_handle_qrun` (Discord) have the same dual-path issue.

## GitLab

- **This project uses GitLab. Do not use `gh` (GitHub CLI).**
- Use `glab` CLI
