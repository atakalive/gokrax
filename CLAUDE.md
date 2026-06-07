# CLAUDE.md — gokrax

## Overview

Development pipeline automation tool. Automates the cycle of Issue creation → design review → implementation → code review → merge. CLI + watchdog daemon architecture.

### Architecture

```
# === CLI ===
gokrax.py              # CLI entry point (parser definitions; subcommand bodies re-exported)
commands/dev/          # Dev mode CLI subcommand implementations (package)
commands/spec.py       # Spec mode CLI subcommands
commands/issue_ops.py  # `issue-update` implementation (GitLab issue body replace)

# === Watchdog daemon ===
watchdog.py            # Main loop (process), Discord handler, queue management
                       # Most logic extracted to engine/

# === engine/ — Core logic extracted from watchdog ===
engine/shared.py       # Shared utilities for watchdog/gokrax
engine/reviewer.py     # Reviewer management (selection, reset, etc.)
engine/agent_meta.py   # Reviewer agent metadata snapshot (provider/model/think_level)
engine/cc.py           # CC CLI automation (plan/impl launch, pytest baseline)
engine/fsm.py          # Dev mode state transitions (check_transition, etc.)
engine/fsm_spec.py     # Spec mode state transitions (check_transition_spec, etc.)
engine/backend.py      # Backend abstraction layer (dispatch)
engine/backend_types.py     # Shared backend return type (SendResult: OK/BUSY/FAIL)
engine/backend_openclaw.py  # openclaw backend implementation
engine/backend_pi.py   # pi (pi-coding-agent) backend implementation
engine/backend_cc.py   # cc backend (via claude CLI)
engine/cci_runner.py   # CCI driver (one-shot TUI, pexpect-based)
engine/backend_cci.py  # cci backend (via claude TUI, subscription-billed)
engine/backend_gemini.py    # Gemini CLI backend implementation
engine/backend_kimi.py      # Kimi CLI backend implementation
engine/backend_agy.py       # agy (Antigravity CLI) backend implementation
engine/gemini_quota.py      # Gemini Pro quota detection & fallback
engine/openai_codex_quota.py # OpenAI Codex (ChatGPT) quota detection & fallback (pi backend)
engine/agy_quota.py         # agy quota detection & fallback (proactive REST)
engine/fallback_cache.py    # Quota fallback cache primitives
engine/glab.py         # GitLab CLI / API wrapper
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
  config_pi.json       # pi backend per-agent configuration
  config_cc.json       # cc backend per-agent configuration
  config_cci.json      # cci backend per-agent configuration
  config_gemini.json   # gemini backend per-agent configuration
  config_kimi.json     # kimi backend per-agent configuration
  config_agy.json      # agy backend per-agent configuration

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
  - **Mock exemption modules:** `test_notify`, `test_config`, `test_short_context`, `test_phase_override`, `test_run_glab`, `test_gemini_quota`, `test_openai_codex_quota`, `test_agy_quota`. These bypass the global mocks because they exercise the internal implementations directly. New quota/notification tests that need real wiring should be added to this exemption list in `tests/conftest.py`.
- **Patch the binding target after `from ... import`, not the source module.** If `watchdog.py` does `from engine.cc import _start_cc`, then `patch("engine.cc._start_cc")` is ineffective. Use `patch("watchdog._start_cc")` instead. `from X import Y` binds at module load time, so patching the source module does not affect existing bindings.
- **Do not call `_reset_reviewers` / `_reset_short_context_reviewers` in tests.** Mocked in conftest. For direct testing, configure mocks individually as in `test_short_context.py`.
- **Tests are hermetic from your `settings.py`.** `tests/conftest.py` sets `GOKRAX_SETTINGS` to the committed `tests/hermetic_settings.py` before `config` is first imported, so the suite never loads your local, `.gitignore`'d `settings.py`. Never write a test that depends on personal settings. To pin a config value for the whole suite, edit `tests/hermetic_settings.py` — the single source of truth for the `TEST_*` constants (`REVIEWERS`, `REVIEW_MODES`, namespace, thresholds, …) that `conftest.py` imports. Do not rely on config defaults or your local settings.
- **The test config is English (`PROMPT_LANG="en"`).** Production renders English under the hermetic config, so assert the English output; do not hardcode Japanese prompt/notification strings in assertions.
- **Register new config-importing modules in conftest.** Corollary of the binding-target rule above: if a module binds config values at import (`from config import REVIEWERS/REVIEW_MODES/...`), add its name to the `for mod_name in (...)` re-patch loop in `_override_config_names` (`tests/conftest.py`), or your test's config overrides won't reach its binding. (This is why `spec_review` and `engine.fsm_spec` are listed there.)
- **Do not hardcode personal data in tests.** No real `GOKRAX_CLI` paths, Discord IDs, or agent names — use the pinned hermetic values / `TEST_*` constants.

## Design Notes

### Pipeline JSON
- **Do not edit pipeline JSON directly.** Always use `update_pipeline()` in `pipeline_io.py`.
- `update_pipeline()` uses flock(LOCK_EX) blocking exclusive lock. Do not use LOCK_NB.
- Pipeline JSON path: `~/.gokrax/pipelines/<project>.json`

### State Transitions
- Valid states and transitions are defined in `config/states.py` (`VALID_STATES` / `VALID_TRANSITIONS`)
- Transitions are executed via `gokrax transition` CLI command or watchdog's `check_transition()`
- Spec mode is a separate system: `SPEC_STATES` / `SPEC_TRANSITIONS` / `check_transition_spec()`

### Watchdog
- `watchdog-loop.sh` polls every 20 seconds
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
- CLI subcommand bodies (`cmd_transition`, `cmd_qrun`, etc.) live in `commands/dev/` (`lifecycle.py`, `queue.py`, ...). `gokrax.py` only re-exports them, so don't grep `gokrax.py` for the implementation — follow the import.
- `cmd_transition` (CLI, `commands/dev/lifecycle.py`) and `do_transition` (watchdog, `watchdog.py`) are separate code paths. Fixing only one may not affect the primary path.
- `cmd_qrun` (CLI, `commands/dev/queue.py`) and `_handle_qrun` (Discord, `watchdog.py`) have the same dual-path issue.

## GitLab

- **This project uses GitLab. Do not use `gh` (GitHub CLI).**
- Use `glab` CLI
