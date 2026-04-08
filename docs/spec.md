# gokrax -- Development Pipeline Specification

> Official specification based on current code (as of 2026-03-29). All agents must follow this document.
> Constant values shown are config defaults. They can be overridden via settings.py (see Chapter 14).

## 1. Overview

gokrax is a CLI + watchdog system that manages the **Issue -> Design -> Implementation -> Test -> Review -> Merge** pipeline. It is a pure orchestrator that does not use LLMs, driving pipeline JSON as a state machine.

## 2. Architecture

```
gokrax.py            -- CLI entry point + watchdog loop management + main()
commands/dev.py       -- Normal mode CLI commands (cmd_start, cmd_review, etc.)
commands/spec.py      -- Spec mode CLI commands (cmd_spec_start, etc.)
config/               -- Packaged module
  __init__.py          -- Centralized constant management + settings.py override
  states.py            -- State transition tables & pipeline constants
  paths.py             -- File path & directory constants
engine/
  fsm.py               -- Normal mode state transition logic (pure functions)
  fsm_spec.py          -- Spec mode state transition logic
  cc.py                -- Claude Code auto-launch & test execution
  reviewer.py          -- Reviewer management (tier, pending, revise decisions)
  shared.py            -- Shared utilities (log, is_cc_running, is_ok_reply)
  backend.py           -- Backend dispatch (openclaw/pi/cc routing)
  backend_openclaw.py  -- openclaw backend (via Gateway CLI)
  backend_pi.py        -- pi backend (via pi CLI)
  backend_cc.py        -- cc backend (via claude CLI)
  cleanup.py           -- Batch state cleanup shared functions
  filter.py            -- Project/author filtering (allowed authors, issue/comment validation)
watchdog.py           -- Watchdog loop + Discord command handling
notify.py             -- Agent notifications + Discord posting (via CLI)
pipeline_io.py        -- JSON read/write (exclusive lock + atomic write)
spec_review.py        -- Spec review parsing & integration
spec_revise.py        -- Spec revision requests & self-review
spec_issue.py         -- Issue breakdown & creation & queue generation
task_queue.py         -- Task queue management (qrun/qadd/qdel/qedit)
messages/             -- Template messages (via render())
  __init__.py          -- render() entry point
  ja/dev/              -- Normal mode (Japanese)
  ja/spec/             -- Spec mode (Japanese)
  en/dev/              -- Normal mode (English)
  en/spec/             -- Spec mode (English)
settings.py           -- User settings (config override)
```

- **Agent communication**: `engine/backend.py` acts as a router, dispatching to the `openclaw`, `pi`, or `cc` backend per agent. Controlled by `DEFAULT_AGENT_BACKEND` and `AGENT_BACKEND_OVERRIDE` in `settings.py`.
- **pipeline JSON**: `~/.gokrax/pipelines/<project>.json`
- **watchdog**: Polls every 20 seconds via `watchdog-loop.sh` (see Chapter 7)
- **Discord notification channel**: Configured via `DISCORD_CHANNEL` in `settings.py`

## 3. Role Definitions

### 3.1 Implementer

- During DESIGN_PLAN phase: reviews and edits Issue body, then runs `plan-done`
- During CODE_REVISE phase: manually fixes code based on P0 findings, then runs `code-revise` (records commit + marks revise complete in one step)
- During IMPLEMENTATION phase: CC is auto-launched (the implementer does not do this manually)

Agents are defined in `settings.py` (`AGENTS` dict). See `settings.example.py` for defaults. Address format: `agent:<name>:main`.

### 3.2 Reviewers

Reviewers are classified into 3 tiers. Tier members are defined in `REVIEWER_TIERS` in `settings.py`. See `settings.example.py` for default structure.

| Tier | Members |
|---|---|
| regular | [] |
| free | [] |
| short-context | [] |

- Receive review requests during DESIGN_REVIEW or CODE_REVIEW
- Post verdicts (APPROVE / P0 / P1 / P2) via `gokrax review` command
- **Must not review their own design/implementation**
- Reviewers are not implementers. Reviewers must never run `plan-done`, `commit`, `design-revise`, or `code-revise`

### 3.3 Owner

- At MERGE_SUMMARY_SENT, a summary is posted to the Discord notification channel. Transitions to DONE via any of:
  - Replying "OK" to the Discord summary
  - Running `gokrax ok --pj <project>` CLI command (`commands/dev.py` `cmd_ok`)
  - Auto-transition when `automerge` flag is enabled
- Runs control commands such as `gokrax start` and `gokrax transition --force`

### 3.4 CC (Claude Code)

- Auto-launched by the watchdog during IMPLEMENTATION phase
- Two-stage process: Plan (model: sonnet) -> Impl (model: sonnet)
- After CC completes, automatically runs `gokrax commit`
- **CC is used only during IMPLEMENTATION. It is not used in other phases**
- If multiple implementers are defined in `AGENTS`, the `implementer` field can be used to switch between them

## 4. State Machine

### Valid States (VALID_STATES)

```
IDLE, INITIALIZE,
DESIGN_PLAN, DESIGN_REVIEW, DESIGN_REVIEW_NPASS, DESIGN_REVISE, DESIGN_APPROVED,
ASSESSMENT, IMPLEMENTATION,
CODE_TEST, CODE_TEST_FIX,
CODE_REVIEW, CODE_REVIEW_NPASS, CODE_REVISE, CODE_APPROVED,
MERGE_SUMMARY_SENT, DONE, BLOCKED
```

> **Note**: When spec mode is enabled, VALID_STATES is merged as `sorted(set(VALID_STATES + SPEC_STATES))`
> in alphabetical order.

### Transition Table (VALID_TRANSITIONS)

```
IDLE         -> [INITIALIZE]
INITIALIZE   -> [DESIGN_PLAN, DESIGN_APPROVED]
DESIGN_PLAN  -> [DESIGN_REVIEW]
DESIGN_REVIEW -> [DESIGN_APPROVED, DESIGN_REVISE, BLOCKED, DESIGN_REVIEW_NPASS]
DESIGN_REVIEW_NPASS -> [DESIGN_APPROVED, DESIGN_REVISE, DESIGN_REVIEW_NPASS]
DESIGN_REVISE -> [DESIGN_REVIEW]
DESIGN_APPROVED -> [ASSESSMENT, IMPLEMENTATION]
ASSESSMENT   -> [IMPLEMENTATION, IDLE]
IMPLEMENTATION  -> [CODE_TEST, CODE_REVIEW]
CODE_TEST       -> [CODE_REVIEW, CODE_TEST_FIX, BLOCKED]
CODE_TEST_FIX   -> [CODE_TEST, BLOCKED]
CODE_REVIEW  -> [CODE_APPROVED, CODE_REVISE, BLOCKED, CODE_REVIEW_NPASS]
CODE_REVIEW_NPASS -> [CODE_APPROVED, CODE_REVISE, CODE_REVIEW_NPASS]
CODE_REVISE  -> [CODE_TEST, CODE_REVIEW]
CODE_APPROVED -> [MERGE_SUMMARY_SENT]
MERGE_SUMMARY_SENT -> [DONE]
DONE         -> [IDLE]
BLOCKED      -> [IDLE]
```

Flow diagram:

```
IDLE -> INITIALIZE -> DESIGN_PLAN -> DESIGN_REVIEW -> DESIGN_APPROVED -> ASSESSMENT -> IMPLEMENTATION
                                    ^              |                                                  |
                                    |              v                                                  v
                                DESIGN_REVISE <----+                                             CODE_TEST
                                                                                                 |       |
                                                                                                 v       v
                                                                                         CODE_REVIEW  CODE_TEST_FIX
                                                                                         |       |       |
                                                                                         v       v       v
                                                                                   CODE_REVISE CODE_APPROVED
                                                                                                 |
                                                                                                 v
                                                                                       MERGE_SUMMARY_SENT
                                                                                                 |
                                                                                                 v
                                                                                               DONE -> IDLE

  * DESIGN_REVIEW, CODE_TEST, CODE_TEST_FIX, and CODE_REVIEW can transition to BLOCKED
  * BLOCKED can only return to IDLE
  * CODE_REVISE transitions to CODE_TEST (re-test) or CODE_REVIEW (when testing is not required)
  * When skip_design is enabled: INITIALIZE -> DESIGN_APPROVED (skips DESIGN_PLAN/REVIEW)
  * When skip_assess is enabled: DESIGN_APPROVED -> IMPLEMENTATION (skips ASSESSMENT)
```

### 4.1 State Details

| State | Responsible | Action | Transition Condition |
|-------|-------------|--------|---------------------|
| IDLE | - | Nothing | `gokrax start` transitions to INITIALIZE |
| INITIALIZE | (automatic) | Project initialization | Transitions to DESIGN_PLAN |
| DESIGN_PLAN | Implementer | Review and edit Issue body, run `plan-done` | All Issues have `design_ready` flag |
| DESIGN_REVIEW | Reviewers | Design review, post via `gokrax review` | `min_reviews` reviews collected |
| DESIGN_REVISE | Implementer | Edit Issue body based on P0 findings, run `design-revise` | All target Issues have `design_revised` flag |
| DESIGN_APPROVED | (auto-transition) | When skip_assess is enabled, transitions immediately to IMPLEMENTATION. Otherwise transitions to ASSESSMENT | - |
| ASSESSMENT | CC (automatic) | Determines difficulty and domain risk per Issue | Transitions to IMPLEMENTATION when all Issues are assessed. Transitions to IDLE when only domain_risk-excluded Issues remain |
| IMPLEMENTATION | CC (automatic) | CC auto-launch -> Plan + Impl -> `commit` | All Issues have `commit` hash |
| CODE_TEST | (automatic) | Automatic test execution (`_start_code_test`) | Transitions to CODE_REVIEW / CODE_TEST_FIX / BLOCKED based on test results |
| CODE_TEST_FIX | CC (automatic) | Fix test failures | Transitions to CODE_TEST after fix |
| CODE_REVIEW | Reviewers | Code review, post via `gokrax review` | `min_reviews` reviews collected |
| CODE_REVISE | Implementer | Fix code based on P0 findings -> `code-revise --hash` | All target Issues have `code_revised` flag |
| CODE_APPROVED | (auto-transition) | Transitions immediately to MERGE_SUMMARY_SENT | - |
| MERGE_SUMMARY_SENT | Owner | Transitions to DONE via Discord "OK" reply / gokrax ok CLI / automerge | Owner OK reply detected |
| DONE | (automatic) | git push + issue close -> IDLE | Auto-transition |
| BLOCKED | Owner | Manual recovery required | `transition --force --to IDLE` |

### 4.2 Auto-Transition States

- **DESIGN_APPROVED**: Transitions to ASSESSMENT as soon as detected by watchdog (directly to IMPLEMENTATION when skip_assess is enabled).
- **ASSESSMENT**: CC determines complexity level and domain risk per Issue. Transitions to IMPLEMENTATION when assessment is complete. Transitions to IDLE when only domain-risk-excluded Issues remain.
- **CODE_APPROVED**: Transitions immediately to MERGE_SUMMARY_SENT as soon as detected by watchdog. Summary is auto-posted.
- **DONE**: git push + issue close -> IDLE

### 4.3 REVISE Loop

- When P0/REJECT is present, transitions from REVIEW -> REVISE
- After REVISE completes, non-APPROVE reviews (P0/P1/P2/REJECT) are cleared (only APPROVE is preserved)
- During re-review, Issue x Reviewer pairs that already have APPROVE are skipped
- **Maximum 4 cycles** (`MAX_REVISE_CYCLES = 4`). Exceeding this transitions to BLOCKED

## 5. Review Modes

A default value is set per project. Per-batch settings are also possible. Controls the reviewer composition and minimum review count.

Review modes are defined in `REVIEW_MODES` in `settings.py`. See `settings.example.py` for default structure.

| Mode | Members | min_reviews | grace_period_sec | n_pass |
|------|---------|-------------|-----------------|--------|
| full | Defined in `REVIEW_MODES` in `settings.py` | 4 | 0 | — |
| standard | Defined in `REVIEW_MODES` in `settings.py` | 3 | 0 | — |
| lite | Defined in `REVIEW_MODES` in `settings.py` | 2 | 0 | — |
| min | Defined in `REVIEW_MODES` in `settings.py` | 1 | 0 | — |
| skip | (none) | 0 | 0 (auto-approve) | — |
| standard-x2 | Defined in `REVIEW_MODES` in `settings.py` | 3 | 0 | {reviewer1: 2, reviewer3: 2} |

### n_pass (N-Pass Review Setting)

`n_pass` is an optional setting that can be added to a review mode. It causes specified reviewers to perform multiple passes of review.

```python
"standard-x2": {
    "members": [],
    "min_reviews": 3,
    "n_pass": {"reviewer1": 2, "reviewer3": 2},
}
```

- Reviewers not included in `n_pass` default to 1 pass
- Pass 1 is executed during the normal DESIGN_REVIEW / CODE_REVIEW
- Pass 2 and beyond are executed in *_REVIEW_NPASS states

### N-Pass Review

#### Flow

1. Pass 1 completes during normal DESIGN_REVIEW / CODE_REVIEW
2. If reviewers with `n_pass > 1` exist, transitions to *_REVIEW_NPASS
3. NPASS reviewers receive a lightweight prompt (no re-sending of Issue body/diff)
4. After all NPASS passes complete, `count_reviews()` tallies the final verdict — counting each reviewer's latest verdict as 1 vote (including n_pass=1 reviewers)
5. If any submitted reviewer has P0/P1 → immediate REVISE (no timeout wait required)
6. After REVISE → REVIEW, pass counters are reset. Starts from Pass 1 (does not re-enter NPASS directly)

#### GitLab Note Behavior for Intermediate Passes

- Intermediate pass (pass < target_pass) APPROVE: GitLab note is **skipped**
- Intermediate pass P0/P1/P2: GitLab note is **posted** (to allow developers to see feedback)

#### Timeout

- NPASS uses the same timeout as the base REVIEW state
- On timeout: `count_reviews()` collects all verdicts (incomplete NPASS reviewers retain their Pass 1 verdict), and `_resolve_review_outcome` determines the transition target. P0/P1 → REVISE even on timeout
- NPASS does **not** transition to BLOCKED

#### Forced File Externalization

- Triggered when entering CODE_REVIEW state (inside `notify_reviewers`). Not at queue insertion time
- When reviewers with `n_pass > 1` exist in the review mode, review data is always externalized to a file regardless of message size
- This allows NPASS prompts to reference file paths
- Existing queued batches are not affected until they enter CODE_REVIEW

## 6. Timeouts

### BLOCK_TIMERS

| State | Time Limit | Extendable (EXTENDABLE_STATES) |
|-------|-----------|-------------------------------|
| DESIGN_PLAN | 1800 sec (30 min) | yes |
| DESIGN_REVIEW | 3600 sec (60 min) | no |
| DESIGN_REVISE | 1800 sec (30 min) | yes |
| ASSESSMENT | 1200 sec (20 min) | yes |
| IMPLEMENTATION | 7200 sec (120 min) | yes |
| CODE_TEST | 600 sec (10 min) | no |
| CODE_TEST_FIX | 3600 sec (60 min) | yes |
| CODE_REVIEW | 3600 sec (60 min) | no |
| CODE_REVISE | 1800 sec (30 min) | yes |

### Timeout-Related Constants

| Constant | Value | Description |
|----------|-------|-------------|
| NUDGE_GRACE_SEC | 300 sec | No nudges within this period after a transition |
| EXTEND_NOTICE_THRESHOLD | 300 sec | When remaining time is below this value, extension instructions are appended to nudges |
| INACTIVE_THRESHOLD_SEC | 303 sec | Considered inactive if no updates for this many seconds |
| INACTIVE_THRESHOLD_PLAN_SEC | 600 sec | Nudge interval for implementer during DESIGN_PLAN |

### timeout_extension

- `timeout_extension` field in pipeline JSON (int, unit: seconds)
- Added in engine/fsm.py as `block_sec += data.get("timeout_extension", 0)`
- Extended via `gokrax extend --pj <PJ> --by 600` (default 600 sec)
- Extension count is tracked by `extend_count`. Reset per phase at DONE

## 7. Watchdog Behavior

### 7.0 watchdog-loop.sh

- Execution: Polls every 20 seconds via `watchdog-loop.sh`
- PID file: `/tmp/gokrax-watchdog-loop.pid`
- Lock file: `/tmp/gokrax-watchdog-loop.lock`

### 7.1 Main Loop

1. Scans all `*.json` files in `PIPELINES_DIR`
2. Skips if `enabled=false`
3. Determines the next action via `check_transition()` (pure function, no side effects)
4. Double-Checked Locking: re-evaluates within lock + transitions
5. Notifications (Discord, agent sending) happen outside the lock

### 7.2 Agent Communication Method

Agent sending goes through `send()` / `ping()` in `engine/backend.py`. The backend is determined per agent via `resolve_backend()`:
- **openclaw**: `engine/backend_openclaw.py` — sends to Gateway via `openclaw gateway call` CLI
- **pi**: `engine/backend_pi.py` — sends via `pi` CLI. Activity is determined by session file mtime
- **cc**: `engine/backend_cc.py` — sends via `claude -p` CLI. Activity is determined by PID validity and session JSONL mtime

The backend is set via `DEFAULT_AGENT_BACKEND` in `settings.py` (config default: `"openclaw"`, `settings.example.py` recommended: `"pi"`; 3 backends available: openclaw, pi, cc), with per-agent override via `AGENT_BACKEND_OVERRIDE`.

`send_to_agent()` and `send_to_agent_queued()` are the same function (the latter is an alias).
Sends `chat.send` to Gateway via `openclaw gateway call` CLI.

- CLI handles device identity and all auth modes internally
- For `params_json` under `MAX_CLI_ARG_BYTES` only. Messages exceeding this must be externalized to a file by the caller

Per-OS CLI argument size limits (`_get_max_cli_arg_bytes`):

| OS | Threshold | Rationale |
|---|---|---|
| Linux | 120,000 bytes | MAX_ARG_STRLEN=131,072 (single argument limit) |
| macOS | 900,000 bytes | ARG_MAX=1,048,576 (argv+envp total limit) |
| Windows | 30,000 bytes | CreateProcess=32,767 characters |

- Queued in the collect queue, processed as a followup turn after run completion
  - Design prioritizes abort avoidance over immediacy. /new and review requests are also processed as followup turns
- Preserves newlines
- No dependency on files inside `dist/`

### 7.3 Nudges

- **Implementer**: Sends `"continue"` via `send_to_agent_queued()` only when inactive (no updates for INACTIVE_THRESHOLD_SEC=303 seconds or more)
- **Reviewers**: Sends `"continue"` via `send_to_agent_queued()` to incomplete reviewers. Retries after 10 minutes on send failure
- Treated as active while CC is running (`/proc/<pid>` exists)

### 7.4 CC Auto-Launch (IMPLEMENTATION Only)

- On DESIGN_APPROVED -> IMPLEMENTATION transition: `run_cc=True` -> async launch via `_start_cc()`
- If CC dies (`_is_cc_running()=False`): restarted on the watchdog's next cycle
- **CC is not auto-launched during DESIGN_PLAN.** Reviewing Issues is the implementer's responsibility

### 7.5 /new Send Timing

- **On DESIGN_PLAN transition**: Session reset (`/new`) sent to all reviewers
- **On IMPLEMENTATION transition**: Same + implementer also reset (only when PJ changes)
- **On REVISE -> REVIEW transition**: `/new` is not sent (context is preserved)

### 7.6 Discord Notifications

- All state transitions are posted to the Discord notification channel (format: `[PJ] OLD -> NEW (timestamp)`)
- Issue list is posted as a separate message only at DESIGN_PLAN start
- CC progress: Plan start -> Plan complete -> Impl start -> Impl complete
- Merge summary: All Issue x Reviewer verdicts posted as a list

## 8. Pipeline JSON Structure

Pipeline JSON fields are classified into 3 categories.

### 8.1 Initialization Fields (Set by cmd_init / cmd_start)

| Field | Type | Description |
|---|---|---|
| project | str | Project name |
| gitlab | str | GitLab repository path |
| repo_path | str | Local repository path |
| state | str | Current state |
| enabled | bool | Whether monitored by watchdog |
| batch | list[dict] | Issue batch (max MAX_BATCH=5) |
| review_mode | str | Review mode name |
| implementer | str | Implementer agent name |
| automerge | bool | Whether auto-merge is enabled |
| history | list[dict] | Transition history (max MAX_HISTORY=100) |
| created_at | str | Creation timestamp |
| updated_at | str | Update timestamp |

### 8.2 Dynamic Fields (Added/Updated During Execution)

| Field | Type | Description |
|---|---|---|
| cc_pid | int \| null | CC process ID |
| cc_session_id | str \| null | CC session ID |
| design_revise_count | int | Design REVISE cycle count |
| code_revise_count | int | Code REVISE cycle count |
| summary_message_id | str \| null | Discord message ID of the merge summary |
| skip_cc_plan | bool | Skip CC Plan phase |
| skip_test | bool | Skip CODE_TEST |
| keep_ctx_batch | bool | Preserve context across batches |
| keep_ctx_intra | bool | Preserve context within a batch |
| base_commit | str \| null | Base commit hash |
| p2_fix | bool | P2 fix mode |
| comment | str \| null | Comment (instructions passed to CC, etc.) |
| timeout_extension | int | Timeout extension in seconds |
| extend_count | int | Extension count |
| excluded_reviewers | list[str] | Excluded reviewer list |
| min_reviews_override | int \| null | Override value for min_reviews |
| test_result | str \| null | Test result |
| test_output | str \| null | Test output |
| test_retry_count | int | Test retry count |
| test_baseline | dict \| null | pytest baseline data |
| max_design_revise_cycles | int \| null | Per-pipeline design revise cycle limit (incremented by --resume) |
| max_code_revise_cycles | int \| null | Per-pipeline code revise cycle limit (incremented by --resume) |

### 8.3 Spec Mode Fields

| Field | Type | Description |
|---|---|---|
| spec_mode | bool | Spec mode enabled flag (set by commands/spec.py cmd_spec_start) |
| spec_config | dict | Spec mode configuration (initialized by pipeline_io.default_spec_config) |

See `docs/spec_mode_spec.md` for spec_config details.

### 8.4 Sample

```json
{
  "project": "MyProject",
  "gitlab": "username/MyProject",
  "repo_path": "/path/to/MyProject",
  "state": "IDLE",
  "enabled": false,
  "implementer": "implementer1",
  "review_mode": "standard",
  "automerge": false,
  "batch": [
    {
      "issue": 17,
      "title": "Issue title",
      "commit": null,
      "cc_session_id": null,
      "design_ready": false,
      "design_reviews": {},
      "code_reviews": {},
      "design_revised": false,
      "code_revised": false,
      "added_at": "..."
    }
  ],
  "history": [{"from": "IDLE", "to": "DESIGN_PLAN", "at": "...", "actor": "cli"}],
  "cc_pid": null,
  "cc_session_id": null,
  "timeout_extension": 0,
  "extend_count": 0,
  "design_revise_count": 0,
  "code_revise_count": 0,
  "summary_message_id": null,
  "created_at": "...",
  "updated_at": "..."
}
```

## 9. Verdict Definitions

| Verdict | Meaning | Effect |
|---------|---------|--------|
| APPROVE | Approved | Counted. Skipped during re-review |
| P0 | Mandatory fix (blocker) | Triggers REVISE transition. Cleared after revise |
| P1 | Minor finding (non-blocking) | Counted. Cleared after revise |
| P2 | Trivial finding (improvement suggestion) | Counted. Cleared after revise |
| REJECT | Rejected | Equivalent to P0 |

Valid verdicts: `VALID_VERDICTS = ["APPROVE", "P0", "P1", "P2", "REJECT"]`
Flag verdicts: `VALID_FLAG_VERDICTS = ["P0", "P1", "P2"]`

## 10. Testing Principles

### Tests Must Never Affect the Production Environment

This is the highest priority rule. Tests must never touch the production watchdog, crontab, pipeline JSON, Discord notifications, or agent sessions.

#### Required Isolation Measures (conftest.py `_block_external_calls`)

| Target | Mock Method | Reason |
|---|---|---|
| `notify.post_discord` | `return_value="mock-msg-id"` | Do not call the Discord API |
| `notify.send_to_agent` | `return_value=True` | Do not call the Gateway CLI |
| `notify.send_to_agent_queued` | `return_value=True` | Same (alias) |
| `notify.ping_agent` | `return_value=True` | Do not call ping |
| `watchdog.send_to_agent` | `return_value=True` | Same |
| `watchdog.send_to_agent_queued` | `return_value=True` | Same |
| `watchdog.ping_agent` | `return_value=True` | Same |
| `engine.reviewer._reset_reviewers` | `return_value=[]` | Do not execute reviewer reset |
| `engine.reviewer._reset_short_context_reviewers` | mocked | Same |
| `time.sleep` | mocked | Prevent cumulative test timeout |
| `config.PIPELINES_DIR` | monkeypatched to `tmp_path` | Do not touch production pipeline JSON |
| `config.LOG_FILE` / `watchdog.LOG_FILE` | redirected to `tmp_path` | Do not pollute production logs |

#### When Adding New External Side Effects

1. If you add a function that affects the production environment (file writes, process operations, API calls), **add a mock to conftest.py**
2. After running tests, verify the production watchdog is alive: `cat /tmp/gokrax-watchdog-loop.pid && ps -p $(cat /tmp/gokrax-watchdog-loop.pid)`

#### Prohibited Test Practices

- Do not call `time.sleep()` directly in test code. `time.sleep` is globally mocked in conftest
- To verify sleep behavior, use `patch("time.sleep") as mock_sleep` and assert call count/arguments
- Do not execute external communication (Discord, agent sending) in tests. Mocked in conftest via `_block_external_calls`
- Do not execute `_reset_reviewers` / `_reset_short_context_reviewers` in tests. Mocked in conftest

## 11. Prohibited Actions

1. **Do not directly edit pipeline JSON.** Always operate via gokrax CLI or `pipeline_io.update_pipeline()`
2. **An implementer must not review (APPROVE) their own design/implementation**
3. **A reviewer must not run `plan-done`, `commit`, `design-revise`, or `code-revise`** (role violation)
4. **Do not manually launch CC during DESIGN_PLAN.** Reviewing Issues is the implementer's responsibility
5. **When manually transitioning states with the watchdog disabled, the `--force` flag is required**
6. **Do not manually tamper with CODE_TEST / CODE_TEST_FIX results.** Tests are executed automatically by the pipeline

## 12. Task Queue

### Overview

`task_queue.py` manages the task queue. The queue file is `gokrax-queue.txt`.

### Commands

| Command | Description |
|---|---|
| qrun | Execute the first task in the queue |
| qadd | Add a task to the queue |
| qdel | Remove a task from the queue |
| qedit | Edit a task in the queue |
| qstatus | Display queue status |

### Queue Line Format

`parse_queue_line()` parse format:

```
PROJECT ISSUES [MODE] [OPTIONS...]
```

| Option | Description |
|---|---|
| automerge | Enable auto-merge |
| plan=MODEL | Specify model for Plan phase |
| impl=MODEL | Specify model for Impl phase |
| comment=TEXT | Instruction comment for CC |
| keep-ctx-batch | Preserve context across batches |
| keep-ctx-intra | Preserve context within a batch |
| keep-ctx-all | Preserve context across and within batches |
| p2-fix | P2 fix mode |
| skip-cc-plan | Skip CC Plan phase |
| skip-test | Skip CODE_TEST |
| skip-assess | Skip ASSESSMENT |
| skip-design | Skip DESIGN_PLAN/REVIEW |
| no-cc | Implementer works directly without CC |
| exclude-high-risk | Exclude Issues with domain_risk=high |
| exclude-any-risk | Exclude Issues with domain_risk other than none |

- Queue operations are executed atomically with `fcntl` locks

## 13. CODE_TEST Gate

### Overview

A gate that automatically runs tests after IMPLEMENTATION completion, before CODE_REVIEW. Implemented in `engine/cc.py`.

### Behavior

- `_start_code_test()`: Runs the test command in the background
- `_poll_code_test()`: Polls for test results
- Test pass: Transitions to CODE_REVIEW
- Test fail: Transitions to CODE_TEST_FIX (CC attempts automatic fix)
- Maximum retries: `MAX_TEST_RETRY = 4`

### Test Configuration

- `TEST_CONFIG` defines per-project test configuration
- `test_baseline` field holds pytest baseline data
- Tests can be skipped with `skip_test=True`

## 14. settings.py Override

### Mechanism

At the end of `config/__init__.py`, `settings.py` is dynamically loaded and uppercase variables are used to override config globals.

```python
_settings_path = Path(os.environ["GOKRAX_SETTINGS"]) if "GOKRAX_SETTINGS" in os.environ \
    else Path(__file__).resolve().parent.parent / "settings.py"
if _settings_path.exists():
    _spec = _importlib_util.spec_from_file_location("_gokrax_settings", _settings_path)
    _settings_mod = _importlib_util.module_from_spec(_spec)
    _spec.loader.exec_module(_settings_mod)
    for _attr in dir(_settings_mod):
        if _attr.isupper() and not _attr.startswith("_"):
            globals()[_attr] = getattr(_settings_mod, _attr)
```

### Usage

- Place `settings.py` in the project root and define uppercase constants to override
- Alternatively, specify the path via the `GOKRAX_SETTINGS` environment variable
- Overridable targets: AGENTS, REVIEWER_TIERS, REVIEW_MODES, BLOCK_TIMERS, MAX_REVISE_CYCLES, and all other uppercase constants defined in config
- All constant values in this specification are config defaults. Actual values may differ if overridden in settings.py

## 15. Message Templates

### Overview

Prompt and notification templates are externalized in the `messages/` directory.

### Structure

```
messages/
  __init__.py    -- render() entry point
  ja/dev/        -- Normal mode (Japanese)
  ja/spec/       -- Spec mode (Japanese)
  en/dev/        -- Normal mode (English)
  en/spec/       -- Spec mode (English)
```

### Usage

- Call `messages.render()` to retrieve templates
- Switch template language via `PROMPT_LANG` in `settings.py` (default: `"en"`)
- Template categories: design_plan, design_review, code_review, code_revise, implementation, code_test_fix, blocked, etc.

## 16. Spec Mode Overview

Spec mode has a separate state transition system from the normal development pipeline. It automates spec review, revision, and Issue creation.

- States: `SPEC_STATES` (defined in config/states.py)
- Transitions: `SPEC_TRANSITIONS` (defined in config/states.py)
- Transition logic: `check_transition_spec()` in `engine/fsm_spec.py`
- CLI: `cmd_spec_start` etc. in `commands/spec.py`
- spec_config initialization: `pipeline_io.default_spec_config()`

See `docs/spec_mode_spec.md` for details.
