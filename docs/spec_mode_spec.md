# gokrax Spec Mode — Specification

**Version:** 8.0 (Based on current code as of 2026-03-20)
**Date:** 2026-03-20

---

### Changelog

| Version | Date | Description |
|---|---|---|
| 1.0 | 2026-02-28 | Initial version (821 lines) |
| 2.0 | 2026-02-28 | rev1 review feedback (42 items) |
| 3.0 | 2026-02-28 | rev2 review feedback (24 items) + 3 Owner directives |
| 4.0 | 2026-03-01 | rev3 review feedback (Pascal 4, Leibniz 7, Dijkstra 11 → 15 after dedup) |
| 5.0 | 2026-03-01 | rev4 review feedback (Pascal 3, Leibniz 5, Dijkstra 4 → 8 after dedup) |
| 6.0 | 2026-03-01 | rev5 review feedback (Pascal 2, Leibniz 5, Dijkstra 2 → 9 items) |
| 7.0 | 2026-03-01 | rev6 review feedback (Pascal 2, Leibniz 5, Dijkstra 1 → 7 items). **Zero Criticals achieved** |
| 8.0 | 2026-03-20 | Post-implementation code sync (v7→v8: constants updated, submit commands added, §13 replaced with implementation results) |

**Key changes in v7→v8:**

- **[v8] §2.4 Constants updated**: MAX_SPEC_REVISE_CYCLES=10 (5→10), MIN_VALID_REVIEWS_BY_MODE values updated (full=4, standard=3, lite=2, lite3=3, lite3_woGoogle=3)
- **[v8] §3.1 auto_qrun field added**: auto_qrun added to default_spec_config(), max_revise_cycles default 10
- **[v8] §3.2 Timeout constants added**: SPEC_ISSUE_PLAN_TIMEOUT_SEC=1800, SPEC_QUEUE_PLAN_TIMEOUT_SEC=1800
- **[v8] §3.3 CLI→pipeline mapping added**: --auto-qrun, --rev
- **[v8] §4.1 Submit commands added**: review-submit, revise-submit, self-review-submit, issue-submit, queue-submit, suggestion-submit (6 commands)
- **[v8] §4.2 CLI flags added**: --auto-qrun, --rev N, --review-mode choices restricted (full/standard/lite/min only)
- **[v8] §10.1 SpecTransitionAction extended**: nudge_reviewers, nudge_implementer fields added
- **[v8] §10.2 Timeout table updated**: ISSUE_PLAN/QUEUE_PLAN timeout values specified
- **[v8] §13 Replaced with implementation results summary**: Plan → actual post-implementation file listing
- **[v8] Known limitations noted**: Unregistered mode issue in MIN_VALID_REVIEWS_BY_MODE

---

See "Appendix A" at the end of this document for detailed v1→v2 and v2→v3 changelogs.

---

## 1. Purpose and Background

### 1.1 Current Problem

The spec creation, review, and revision cycle is currently entirely manual:

1. spec_implementer (agent specified in settings) creates a spec draft in collaboration with the Owner
2. Send individually to 3 reviewers via `sessions_send`
3. Wait for review results
4. Manually analyze, deduplicate, and merge results from all 3 reviewers
5. Manually revise the spec file (revN → revN+1)
6. git commit & push
7. Repeat steps 2–6 (5+ rounds for some specs)
8. Manually create GitLab Issues from the finalized spec (10+ issues for some specs)
9. Manually write batch execution order in gokrax-queue.txt

### 1.2 Goal

Add **spec mode** to gokrax to automate steps 2–9 above.

### 1.3 Scope

**In scope:** Spec review cycle automation, semi-automated Issue breakdown, queue generation automation

**Out of scope:** Automated spec draft generation, direct connection to gokrax implementation flow, bootstrapping

---

## 2. State Machine

### 2.1 State Transition Diagram

```
[gokrax spec start]
        │
        ├─── [--skip-review] ───→ SPEC_APPROVED
        │
        ▼
  SPEC_REVIEW ◄──────────────┐
        │                    │
        │ (Valid reviews     │
        │  collected         │
        │  or timeout)       │
        ▼                    │
  ┌─ SPEC_REVISE ────────────┘  ← [v4] REVISE always returns to REVIEW
  │
  │ (After SPEC_REVIEW completion: no P1+ findings)
  │     ▼
  │   SPEC_APPROVED ──── [--review-only] ───→ SPEC_DONE
  │     │
  │     │ [gokrax spec continue] or [--auto-continue]
  │     ▼
  │   ISSUE_SUGGESTION
  │     │
  │     ▼
  │   ISSUE_PLAN
  │     │
  │     ▼
  │   QUEUE_PLAN ─── [--no-queue] ───→ SPEC_DONE
  │     │
  │     ▼
  │   SPEC_DONE ──── [gokrax spec done] ───→ IDLE
  │
  │ (MAX_CYCLES reached & P1+ remaining)
  └──→ SPEC_STALLED ─→ [spec extend] → SPEC_REVISE (increase MAX, revise with existing findings)
                   └─→ [spec approve --force] → SPEC_APPROVED

  * Error states:
  SPEC_REVIEW_FAILED ←── (0 valid reviews, all timed out)
  SPEC_PAUSED ←── (MAX_RETRIES exceeded / parse failure + insufficient valid / unknown state)
```

### 2.2 State Definitions

| State | Description | Exit |
|---|---|---|
| `SPEC_REVIEW` | Spec sent to reviewers, awaiting collection | Determined by should_continue_review() (§5.3) |
| `SPEC_REVISE` | Merged report generated, implementer revises | Commit completed → SPEC_REVIEW |
| `SPEC_APPROVED` | Revision cycle completed | auto_continue → ISSUE_SUGGESTION / default → awaiting Owner confirmation / --review-only → DONE |
| `ISSUE_SUGGESTION` | Querying reviewers for Issue breakdown proposals | Collection completed → ISSUE_PLAN |
| `ISSUE_PLAN` | Implementer merges proposals → creates GitLab Issues | Issue creation completed → QUEUE_PLAN |
| `QUEUE_PLAN` | gokrax-queue.txt generation | Generation completed → DONE |
| `SPEC_DONE` | All stages completed, awaiting Owner final confirmation | `spec done` → IDLE |
| `SPEC_STALLED` | MAX_CYCLES reached & P1+ remaining, Owner intervention required | extend → REVISE / --force → APPROVED |
| `SPEC_REVIEW_FAILED` | 0 valid reviews (all timed out) | `spec retry` → REVIEW |
| `SPEC_PAUSED` | Retry exceeded / parse failure + insufficient valid / anomaly | `spec resume` → paused_from |
| `IDLE` | Inactive | — |

### 2.3 Coexistence with Existing States

```python
SPEC_STATES = [
    "SPEC_REVIEW", "SPEC_REVISE", "SPEC_APPROVED",
    "ISSUE_SUGGESTION", "ISSUE_PLAN", "QUEUE_PLAN", "SPEC_DONE",
    "SPEC_STALLED", "SPEC_REVIEW_FAILED", "SPEC_PAUSED",
]
VALID_STATES = VALID_STATES + SPEC_STATES

SPEC_TRANSITIONS = {
    "IDLE":                 ["SPEC_REVIEW", "SPEC_APPROVED"],
    "SPEC_REVIEW":          ["SPEC_REVISE", "SPEC_APPROVED", "SPEC_STALLED",
                             "SPEC_REVIEW_FAILED", "SPEC_PAUSED"],
    # [v4] REVISE can only go to REVIEW or PAUSED. Cannot go directly to APPROVED
    "SPEC_REVISE":          ["SPEC_REVIEW", "SPEC_PAUSED"],
    "SPEC_APPROVED":        ["ISSUE_SUGGESTION", "SPEC_DONE"],
    "ISSUE_SUGGESTION":     ["ISSUE_PLAN", "SPEC_PAUSED"],
    "ISSUE_PLAN":           ["QUEUE_PLAN", "SPEC_DONE", "SPEC_PAUSED"],
    "QUEUE_PLAN":           ["SPEC_DONE", "SPEC_PAUSED"],
    "SPEC_DONE":            ["IDLE"],
    # [v5] Pascal P-1: extend→REVISE direct (avoid idle review round)
    "SPEC_STALLED":         ["SPEC_APPROVED", "SPEC_REVISE"],
    "SPEC_REVIEW_FAILED":   ["SPEC_REVIEW"],
    "SPEC_PAUSED":          ["SPEC_REVIEW", "SPEC_REVISE", "SPEC_APPROVED",
                             "ISSUE_SUGGESTION", "ISSUE_PLAN", "QUEUE_PLAN",
                             "SPEC_DONE"],
}

# [v4] Leibniz C-3: sorted(set(...)) for deterministic order
for state, targets in SPEC_TRANSITIONS.items():
    existing = VALID_TRANSITIONS.get(state, [])
    VALID_TRANSITIONS[state] = sorted(set(existing + targets))

STATE_PHASE_MAP.update({s: "spec" for s in SPEC_STATES})
```

**Mutual exclusion:** `gokrax spec start` acquires an flock exclusive lock on pipeline.json → atomically sets `spec_mode = true`. While `spec_mode = true`, existing `gokrax start` / `gokrax transition` return errors. `spec_mode = false` is cleared on transition to IDLE.

### 2.4 Termination Conditions (Summary)

<!-- [v4] Leibniz M-1: §2.4 is a summary. The canonical logic is in §5.3 -->

The post-SPEC_REVIEW determination is solely governed by `should_continue_review()` (§5.3). The following is a summary:

| Condition | Result |
|---|---|
| 0 valid reviews (all timed out) | REVIEW_FAILED |
| Parse failure present & valid < MIN | PAUSED |
| Valid < MIN (no parse failures) | REVIEW_FAILED |
| No P1+ findings (P0/P1/P2) | APPROVED |
| MAX reached & P1+ findings (P0/P1/P2) | STALLED |
| P1+ findings (P0/P1/P2) & MAX not reached | REVISE (→ revision → REVIEW) |

**Constants:**
- `MAX_SPEC_REVISE_CYCLES = 10`
- `MIN_VALID_REVIEWS`: Follows review_mode. full=4, standard=3, lite=2, min=1, skip=0

### 2.5 Early Termination Options

| --skip-review | --review-only | --no-queue | --auto-continue | Start | End | Owner confirmation | Use case |
|---|---|---|---|---|---|---|---|
| ✗ | ✗ | ✗ | ✗ | REVIEW | DONE | At APPROVED | Full pipeline (default) |
| ✗ | ✗ | ✗ | ✓ | REVIEW | DONE | None | Full pipeline (auto-proceed) |
| ✗ | ✓ | — | (ignored) | REVIEW | DONE | None | Review only |
| ✗ | ✗ | ✓ | ✗ | REVIEW | DONE | At APPROVED | Up to Issue creation |
| ✓ | ✗ | ✗ | (forced ✓) | APPROVED | DONE | None | Issue creation + queue |
| ✓ | ✗ | ✓ | (forced ✓) | APPROVED | DONE | None | Issue creation only |
| ✓ | ✓ | — | — | **Error** | — | — | Meaningless |

### 2.6 CLI Option Precedence

<!-- [v4] Leibniz m-1 -->

| Condition | Override rule |
|---|---|
| `--skip-review` | Forces `auto_continue` to true |
| `--review-only` | Forces `auto_continue` to false (ignored), forces `no_queue` to true |
| `--review-only` + `--auto-continue` | `review_only` wins (auto_continue ignored) |
| `--skip-review` + `--review-only` | **Error** |

These overrides are applied within `gokrax spec start`, before writing to pipeline.json.

---

## 3. Pipeline Configuration

### 3.1 pipeline.json Extension

Spec mode does **not use** the existing `batch[]`. Everything is stored in `spec_config`.

```json
{
  "project": "gokrax",
  "state": "SPEC_REVIEW",
  "spec_mode": true,
  "spec_config": {
    "spec_path": "docs/spec-mode-spec.md",
    "spec_implementer": "implementer1",
    "review_only": false,
    "no_queue": false,
    "skip_review": false,
    "auto_continue": false,
    "auto_qrun": false,
    "self_review_passes": 2,
    "self_review_agent": null,
    "current_rev": "1",
    "rev_index": 1,
    "max_revise_cycles": 10,
    "revise_count": 0,
    "last_commit": null,
    "model": null,
    "review_requests": {},
    "current_reviews": {},
    "issue_suggestions": {},
    "created_issues": [],
    "review_history": [],
    "force_events": [],
    "retry_counts": {},
    "paused_from": null,
    "pipelines_dir": null,
    "last_changes": null
  },
  "enabled": true,
  "review_mode": "full",
  "batch": []
}
```

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| spec_path | str | ✅ | — | Repository-relative path to the spec file |
| spec_implementer | str | ✅ | — | Revision agent ID |
| review_only | bool | — | false | Review cycle only (skip Issue breakdown and queue) |
| no_queue | bool | — | false | Skip queue generation |
| skip_review | bool | — | false | Skip review |
| auto_continue | bool | — | false | Auto-proceed to ISSUE_SUGGESTION after SPEC_APPROVED without Owner confirmation |
| auto_qrun | bool | — | false | Auto-start qrun after SPEC_DONE |
| self_review_passes | int | — | 2 | Number of self-review passes |
| self_review_agent | str\|null | — | null | Pass 2 agent (null = first reviewer in the list) |
| current_rev | str | — | "1" | Revision ("1", "2", "2A", etc.) |
| rev_index | int | — | 1 | Sequential number for ordering |
| max_revise_cycles | int | — | 10 | Maximum revision cycles |
| revise_count | int | — | 0 | Completed revision cycle count |
| last_commit | str\|null | — | null | Previous rev's commit hash |
| model | str\|null | — | null | Implementer model reference info |
| review_requests | dict | — | {} | Per-reviewer timeout management (§5.1) |
| current_reviews | dict | — | {} | Persisted parse results for the current round |
| issue_suggestions | dict | — | {} | Issue breakdown proposals |
| created_issues | list[int] | — | [] | Created Issue numbers |
| review_history | list | — | [] | Round result summaries |
| force_events | list | — | [] | approve --force audit log |
| retry_counts | dict | — | {} | Per-state retry counts |
| paused_from | str\|null | — | null | PAUSED return destination |
| pipelines_dir | str\|null | — | null | Absolute path for raw review file storage |
| last_changes | dict\|null | — | null | <!-- [v6] Pascal P-1 --> Previous revision's changes object (for prompt generation) |

**current_reviews structure:**
<!-- [v7] Leibniz M-1: meta/body separation -->
```json
"current_reviews": {
  "reviewed_rev": "2",
  "entries": {
    "reviewer1": {
      "verdict": "P0",
      "items": [...],
      "raw_text": "...",
      "parse_success": true,
      "status": "received"
    },
    "reviewer2": {
      "verdict": null,
      "items": [],
      "raw_text": null,
      "parse_success": false,
      "status": "timeout"
    }
  }
}
```

`reviewed_rev`: The revision that this review set targets. Even during extend→REVISE direct transition, reviews are treated as associated with this rev. Used as the rev number when archiving to review_history.

**Per-reviewer `status` transition rules:**
<!-- [v7] Leibniz M-3 -->
```
pending → received    (response received + parse success)
pending → timeout     (SPEC_REVIEW_TIMEOUT_SEC exceeded)
pending → parse_failed (response received + parse failure)
```

**Required fields when received (invariants):**
- `verdict` ∈ {"APPROVE", "P0", "P1"} (required, cannot be null. Note: limited to values accepted by `VERDICT_ALIASES`. Internally P2 also functions as a revise trigger in `should_continue_review()`, but current `parse_review_yaml()` does not produce P2)
- `items`: list[SpecReviewItem] (empty list allowed)
- `parse_success` = true

If these invariants are violated, fall back to `status='parse_failed'`.

Data is not lost on recovery from PAUSED/restart. At round completion, data is moved to review_history and current_reviews is cleared.

**retry_counts structure and rules:**
```json
"retry_counts": {
  "SPEC_REVISE": 1,
  "ISSUE_PLAN": 0
}
```
**Increment conditions (when to +1):**
- SPEC_REVISE: Implementer response timeout
- ISSUE_PLAN: Implementer response timeout
- QUEUE_PLAN: Implementer response timeout

**When NOT to increment:**
- SPEC_REVIEW: Individual reviewer timeouts (managed per-reviewer)
- ISSUE_SUGGESTION: Individual reviewer timeouts

On state transition, the retry_counts entry for the target state is reset (set to 0). When MAX_SPEC_RETRIES is exceeded, transition from that state to SPEC_PAUSED.

### 3.2 config.py Additional Constants

Review mode definitions can be overridden by `REVIEW_MODES` in `settings.py`. See `settings.example.py` for the default structure.

```python
MAX_SPEC_REVISE_CYCLES = 10
MIN_VALID_REVIEWS_BY_MODE = {
    "full": 4, "standard": 3, "lite": 2, "min": 1, "skip": 0,
}
SPEC_REVIEW_TIMEOUT_SEC = 1800
SPEC_REVISE_TIMEOUT_SEC = 1800
SPEC_ISSUE_SUGGESTION_TIMEOUT_SEC = 600
SPEC_ISSUE_PLAN_TIMEOUT_SEC = 1800
SPEC_QUEUE_PLAN_TIMEOUT_SEC = 1800
SPEC_REVISE_SELF_REVIEW_PASSES = 2
MAX_SPEC_RETRIES = 3
# [v4] Leibniz M-2
SPEC_REVIEW_RAW_RETENTION_DAYS = 30
```

### 3.3 CLI→Pipeline Mapping Table

| CLI flag | Storage location | Type |
|---|---|---|
| --pj | project | str |
| --spec | spec_config.spec_path | str |
| --implementer | spec_config.spec_implementer | str |
| --review-only | spec_config.review_only | bool |
| --no-queue | spec_config.no_queue | bool |
| --skip-review | spec_config.skip_review | bool |
| --max-cycles | spec_config.max_revise_cycles | int |
| --review-mode | review_mode | str |
| --model | spec_config.model | str\|null |
| --auto-continue | spec_config.auto_continue | bool |
| --auto-qrun | spec_config.auto_qrun | bool |
| --rev | spec_config.current_rev, spec_config.rev_index | int→str, int |

※ The precedence rules in §2.6 are applied after the mapping.

---

## 4. CLI Interface

### 4.1 Command Structure

```
gokrax spec start               Start pipeline
gokrax spec approve              Transition to SPEC_APPROVED [--force]
gokrax spec continue             APPROVED → ISSUE_SUGGESTION
gokrax spec done                 DONE → IDLE
gokrax spec retry                FAILED → REVIEW
gokrax spec resume               PAUSED → paused_from
gokrax spec extend               STALLED → REVISE (increase MAX, revise with existing findings)
gokrax spec status               Show status
gokrax spec stop                 Stop
gokrax spec review-submit        Submit review results from a YAML file
gokrax spec revise-submit        Submit SPEC_REVISE completion report from a file
gokrax spec self-review-submit   Submit self-review results from a file
gokrax spec issue-submit         Submit ISSUE_PLAN completion report from a file
gokrax spec queue-submit         Submit QUEUE_PLAN completion report from a file
gokrax spec suggestion-submit    Submit ISSUE_SUGGESTION reviewer proposal from a file
```

### 4.2 gokrax spec start

```
gokrax spec start --pj PROJECT --spec SPEC_PATH --implementer AGENT_ID
                  [--review-only] [--no-queue] [--skip-review]
                  [--max-cycles N] [--review-mode MODE] [--model MODEL]
                  [--auto-continue] [--auto-qrun] [--rev N]
```

**Preconditions:** IDLE state, spec file exists, implementer available, `--skip-review --review-only` is an error

**--review-mode choices constraint:** In spec mode, `--review-mode` argparse choices are restricted to `["full", "standard", "lite", "min"]` (4 options only).

**--rev N:** Specifies the initial `current_rev` / `rev_index` values. Default is 1. Consistency with the rev number in the spec filename is checked (mismatch is an error).

**--auto-qrun:** Flag to auto-start qrun after SPEC_DONE.

**Behavior:**
1. Acquire flock exclusive lock on pipeline.json
2. Apply §2.6 precedence rules
3. Write spec_mode=true + spec_config
4. Record pipelines_dir as absolute path (`PIPELINES_DIR/{project}/spec-reviews/`)
5. Initialize reviewer list in review_requests (all pending)
6. enabled=true
7. --skip-review → SPEC_APPROVED, otherwise → SPEC_REVIEW

### 4.3 gokrax spec approve

```
gokrax spec approve --pj PROJECT [--force]
```

- Without --force: Error if P1+ findings exist
- With --force: Force approval. Performs the following:
  1. Generate §12.2-format summary from current_reviews, append to review_history, clear current_reviews
  2. Record in force_events
  3. Discord audit notification

```json
{
  "at": "2026-02-28T23:00:00+09:00",
  "actor": "owner",
  "from_state": "SPEC_STALLED",
  "rev": "3",
  "rev_index": 3,
  "remaining_p1_items": ["reviewer1:M-2", "reviewer5:C-4"]
}
```

### 4.4 gokrax spec status

```
gokrax [SPEC_REVIEW] rev2 (cycle 1/10, retries: REVISE=0/3)
  spec: docs/spec-mode-spec.md
  implementer: implementer1
  reviewers: reviewer1(✅ P0×1), reviewer5(⏳), reviewer2(⏳)
  min_valid: 4 (full mode)
  auto_qrun: false
  pipelines_dir: ~/.openclaw/shared/pipelines/<project>/spec-reviews/
```

### 4.5 gokrax spec retry

```
gokrax spec retry --pj PROJECT
```

**Precondition:** SPEC_REVIEW_FAILED state only
**Behavior:**
1. _reset_review_requests() (§5.4)
2. Clear current_reviews
3. Transition to SPEC_REVIEW (watchdog resends)

### 4.6 gokrax spec resume

```
gokrax spec resume --pj PROJECT
```

**Precondition:** SPEC_PAUSED state only
**Behavior:**
1. Read paused_from. Error if null
2. Only transition to paused_from is allowed (transition to other states is not permitted)
3. If paused_from is SPEC_REVIEW: _reset_review_requests() (§5.4) + clear current_reviews
4. Recalculate timeout_at for all pending entries in review_requests based on current time
5. If paused_from is ISSUE_SUGGESTION: also recalculate timeout_at for pending entries in issue_suggestions
6. Reset retry_counts[paused_from] (to 0)
7. Transition to paused_from, clear paused_from to null

### 4.7 gokrax spec extend

```
gokrax spec extend --pj PROJECT [--cycles N]
```

**Precondition:** SPEC_STALLED state only
**Behavior:**
1. max_revise_cycles += N (default N=2)
2. revise_count is **not** reset
3. <!-- [v5] Pascal P-1 --> current_reviews is **not cleared** (preserve existing findings)
4. → **SPEC_REVISE** (revise immediately with existing findings. Avoids an idle review round)

### 4.8 Submit Commands

The following submit commands inject responses from external agents into the pipeline via files. All require `--pj PROJECT --file FILE` as mandatory arguments. Commands that need a reviewer specification also require `--reviewer REVIEWER`.

| Command | State precondition | Required args | Description |
|---|---|---|---|
| `review-submit` | SPEC_REVIEW | --reviewer | Submit review result YAML (§5.5 format) |
| `revise-submit` | SPEC_REVISE | — | Submit implementer revision completion report (§6.1 format) |
| `self-review-submit` | SPEC_REVISE | — | Submit self-review results (§6.2 format) |
| `issue-submit` | ISSUE_PLAN | — | Submit Issue creation completion report (§8.1 format) |
| `queue-submit` | QUEUE_PLAN | — | Submit queue generation completion report (§9 format) |
| `suggestion-submit` | ISSUE_SUGGESTION | --reviewer | Submit reviewer Issue breakdown proposal (§7 format) |

Each command attempts YAML parsing of the file contents. For unfenced YAML, automatic fence completion (````yaml\n...\n````) is attempted. Idempotency is ensured — duplicate submissions from the same reviewer are skipped.

---

## 5. SPEC_REVIEW Phase

### 5.1 Sending Review Requests

**Integration into watchdog.py process():**
```python
# Allow empty batch when in spec_mode
if state != "DONE" and not batch and not pipeline.get("spec_mode"):
    logger.warning("batch empty, skipping")
    return

if pipeline.get("spec_mode") and state in SPEC_STATES:
    spec_config = pipeline.get("spec_config", {})
    action = check_transition_spec(state, spec_config, now)
    # [v5] Dijkstra M-1 + [v6] Leibniz C-2: Apply if any side-effect field is present
    if action.next_state or action.pipeline_updates or action.send_to or action.discord_notify:
        action.expected_state = state
        _apply_spec_action(pipeline_path, action, now)
    return
```

Review requests are sent to each reviewer via **`send_to_agent()`** (newlines preserved). The spec body is **not embedded**.

<!-- [v5] Leibniz C-2: Postcondition on send -->
**Send function postcondition:** After sending a review request, the target reviewer's `review_requests[reviewer]` must satisfy:
- `sent_at != None` (send timestamp)
- `timeout_at != None` (`sent_at + SPEC_REVIEW_TIMEOUT_SEC`)
- `status == "pending"`

Tests must verify this for all reviewers. Without this guarantee, a pending entry may never time out (or die immediately) — a fatal bug.

**Initial prompt:**

```
Review the following spec. This is an **exhaustive review** request.

Project: {project}
Spec: {spec_path} (rev{current_rev}, {line_count} lines)

## Review Instructions
- Assign severity to every finding: 🔴 Critical (P0) / 🟠 Major (P1) / 🟡 Minor / 💡 Suggestion
- Specify section numbers (e.g. §6.2)
- Pay special attention to consistency between pseudocode sections
- Also verify consistency with the existing gokrax codebase
- Look for state machine transition gaps and deadlocks
- Include only **one** YAML block in your response

## Output Format
```yaml
verdict: APPROVE | P0 | P1
items:
  - id: C-1
    severity: critical | major | minor | suggestion
    section: "§6.2"
    title: "Title"
    description: "Description"
    suggestion: "Suggested fix"
```

## Review Result Storage
`{pipelines_dir}/{YYYYMMDD}T{HHMMSS}_{reviewer}_{spec_name}_rev{current_rev}.md`
```

**Prompt for rev2+:**

diff: `git diff --numstat {last_commit}..HEAD -- {spec_path}`. changelog: Implementer YAML report as primary source.

```
Review the revised version of the following spec.

Project: {project}
Spec: {spec_path} (rev{current_rev})
Changes since last review: +{added_lines} lines, -{removed_lines} lines
Last commit: {last_commit}

## Changes Since Last Review
{changelog_summary}

## Review Instructions
- Verify that previous findings have been properly addressed
- Check new additions for issues
- Severity, section numbers, and YAML format are the same as before
- Include only **one** YAML block in your response

## Review Result Storage
`{pipelines_dir}/{YYYYMMDD}T{HHMMSS}_{reviewer}_{spec_name}_rev{current_rev}.md`
```

### 5.2 Review Collection

```json
"review_requests": {
  "reviewer1": {
    "sent_at": "2026-02-28T21:15:00+09:00",
    "timeout_at": "2026-02-28T21:45:00+09:00",
    "last_nudge_at": null,
    "status": "pending | received | timeout",
    "response": null
  }
}
```

**Collection completion condition:** All reviewer status = received|timeout → call should_continue_review() (§5.3).

**Timeout handling:** Timed-out reviewers are also stored in `current_reviews.entries` with `status='timeout'` (see structure example in §3.1). In should_continue_review(), entries with `status='timeout'` are counted as neither received nor parsed_fail. All timed out → received=0, parsed_fail=0 → "failed".

### 5.3 Termination Decision (Canonical Source)

<!-- [v4] Leibniz M-1: This is the sole decision logic. §2.4 is a summary -->

```python
def should_continue_review(
    spec_config: dict,
    review_mode: str,
    min_reviews_override: int | None = None,
) -> str:  # "revise"|"approved"|"stalled"|"failed"|"paused"
    """The sole decision function called after SPEC_REVIEW completion.
    Data source is spec_config["current_reviews"].

    Raises:
        ValueError: if review_mode is not in MIN_VALID_REVIEWS_BY_MODE
        KeyError: if spec_config is missing rev_index/max_revise_cycles
    """

    cr = spec_config.get("current_reviews", {})
    # [v7] Leibniz M-1: Reviewer dicts stored under entries
    reviewer_entries = cr.get("entries", {})

    # [v8] Only received entries satisfying invariants are considered valid
    received: dict[str, dict] = {}
    parsed_fail: dict[str, dict] = {}
    for k, v in reviewer_entries.items():
        status = v.get("status")
        if status == "received":
            if validate_received_entry(v):
                received[k] = v
            else:
                parsed_fail[k] = v  # Invariant violation → demote to parse_failed
        elif status == "parse_failed":
            parsed_fail[k] = v
        # timeout is counted as neither received nor parsed_fail

    # [v8] Unknown mode raises ValueError (no fallback)
    if review_mode not in MIN_VALID_REVIEWS_BY_MODE:
        raise ValueError(f"Unknown review_mode: {review_mode!r}")
    min_valid = min_reviews_override if min_reviews_override is not None else MIN_VALID_REVIEWS_BY_MODE[review_mode]

    # 1. No one responded (all timed out)
    if len(received) == 0 and len(parsed_fail) == 0:
        return "failed"

    # 2. Valid reviews (received) below threshold
    if len(received) < min_valid:
        if len(parsed_fail) > 0:
            return "paused"   # Parse failures present → human intervention needed
        return "failed"       # Timeout only → recoverable via resend

    # 3. Decide based on valid reviews (revise if any P0/P1/P2)
    # Note: current parse_review_yaml() does not produce P2,
    # but P2 is included to handle injection via normal mode's gokrax review --verdict P2 etc.
    has_p1 = any(v.get("verdict") in ("P0", "P1", "P2") for v in received.values())
    if not has_p1:
        return "approved"

    # 4. MAX reached → stalled (determined based on rev_index)
    if spec_config["rev_index"] >= spec_config["max_revise_cycles"]:
        return "stalled"
    return "revise"
```

**Note:** This function is called only after SPEC_REVIEW completion. After SPEC_REVISE completion, the flow always returns to SPEC_REVIEW (§6.3), so no decision is needed on the REVISE side.

### 5.4 review_requests Reset (Common Helper)

<!-- [v4] Pascal C-2 / Dijkstra m-2: Unified across all paths -->

```python
def _reset_review_requests(spec_config: dict, now: datetime) -> None:
    """Called on all paths transitioning to SPEC_REVIEW."""
    for reviewer, entry in spec_config["review_requests"].items():
        entry["status"] = "pending"
        entry["sent_at"] = None
        entry["timeout_at"] = None
        entry["last_nudge_at"] = None
        entry["response"] = None
```

**Call sites:**
- `gokrax spec start` (initialization)
- `gokrax spec retry` (FAILED→REVIEW)
- `gokrax spec resume` (when paused_from=REVIEW)
- SPEC_REVISE completion returning to REVIEW (§6.3)

<!-- [v5] extend goes STALLED→REVISE so no reset needed (existing current_reviews maintained) -->

### 5.5 Review Result Parsing

**Determinism is the top priority.**

1. Regex extraction of YAML block (first block only)
2. Apply alias mapping to verdict/severity

```python
VERDICT_ALIASES = {
    "approve": "APPROVE",
    "p0": "P0",
    "reject": "P0",
    "p1": "P1",
}
SEVERITY_ALIASES = {
    "critical": "critical",
    "major": "major",
    "minor": "minor",
    "suggestion": "suggestion",
}
```

3. **Invalid values (outside mapping) → parse_success=False, status='parse_failed'**. raw_text is preserved. When storing in current_reviews, status must always be set (see §3.1 status transition rules)

```python
@dataclass
class SpecReviewItem:
    id: str                    # "C-1" (reviewer-local)
    severity: str              # "critical"|"major"|"minor"|"suggestion"
    section: str
    title: str
    description: str
    suggestion: str | None
    reviewer: str
    normalized_id: str         # "reviewer1:C-1"

@dataclass
class SpecReviewResult:
    reviewer: str
    verdict: str               # "APPROVE"|"P0"|"P1" (parser output; P2 is handled internally but not generated by parse_review_yaml)
    items: list[SpecReviewItem]
    raw_text: str
    parse_success: bool

@dataclass
class MergedReviewReport:
    reviews: list[SpecReviewResult]
    all_items: list[SpecReviewItem]
    summary: dict              # {"critical": n, ...}
    highest_verdict: str
```

### 5.6 Deduplication and Merging

**Initial implementation:** No deduplication algorithm is implemented. The merged report lists all findings sorted by severity, and deduplication judgment is delegated to the spec_implementer. Future consideration: embedding similarity-based candidate suggestions.

Merged report format:
```markdown
# Rev{N} Merged Review Report
## Summary
- Reviewers: {reviewer} ({verdict}), ...
- Critical: {n}, Major: {n}, Minor: {n}, Suggestion: {n}
## All Findings (by severity)
### Critical — {normalized_id}: {title} ({section})
### Major — ...
```

---

## 6. SPEC_REVISE Phase

### 6.1 Revision Process

Revision request sent via `send_to_agent()`. The following changelog format is required:
- Add one row to the changelog table
- List all items in the format `[vN] finding ID: description`
- In pseudocode, note change reasons with `# [vN] reviewer1 C-1: description`

Revision completion report YAML:
```yaml
status: done
new_rev: "3"
commit: "abc1234"
changes:
  added_lines: 350
  removed_lines: 50
  reflected_items: ["reviewer1:C-1", "reviewer5:C-1"]
  deferred_items: ["reviewer2:m-4"]
  deferred_reasons:
    "reviewer2:m-4": "Reason"
```

### 6.2 Self-Review

**Pass 1 (implementer themselves):** Check for missed reflections, contradictions, consistency, and changelog

**Pass 2 (different agent):**
- **Selection logic:** If `spec_config.self_review_agent` is set, use that agent. If null, use the first key in review_requests
- **Request prompt:**
```
Cross-check the revised spec.

Spec: {spec_path} (rev{new_rev})
Last commit: {last_commit}

## Check Items
1. Whether reflected_items in the changelog are actually reflected in the body
2. Whether new contradictions or regressions have occurred
3. Type/argument consistency in pseudocode

If no issues in changed sections, report `status: clean`. If fixes are needed, report `status: issues_found` + finding list in YAML.
```
- **Timeout:** SPEC_REVIEW_TIMEOUT_SEC (1800s)
- **On issues_found:** Request re-fix from implementer → commit → re-run Pass 2 (max 1 retry)
- <!-- [v4] Pascal M-1 / Dijkstra M-2 --> **If still issues_found after retry exhaustion:** → SPEC_PAUSED (paused_from="SPEC_REVISE"), Discord notification
- <!-- [v7] Pascal P-2 --> **On resume to SPEC_REVISE:** Restart the process from the §6.1 revision request prompt (not just re-running self-review)

Each pass reports `status: clean | issues_found`.

### 6.3 Revision Completion Detection

<!-- [v4] REVISE → always REVIEW -->

1. Verify YAML `status: done`
2. Self-review Pass 1 + Pass 2 (§6.2. On Pass 2 retry exhaustion → PAUSED)
3. Update last_commit, current_rev, rev_index. Save the implementer's changes object to `last_changes`
4. Verify `added_lines`/`removed_lines` with `git diff --numstat {last_commit}..HEAD -- {spec_path}`. Discord warning if values don't match last_changes (processing continues). In prompts, git diff numstat is the primary source; last_changes changelog_summary is supplementary
5. **revise_count += 1**
6. Generate §12.2-format summary from current_reviews and append to review_history. Clear current_reviews
7. _reset_review_requests() (§5.4)
8. → **SPEC_REVIEW** (always returns to review)

**Difference from existing CODE_REVISE:** Existing triggers revise only on P0. Spec mode **continues the loop on P1+ (P0/P1/P2)**.

---

## 7. ISSUE_SUGGESTION Phase

Transition occurs after the Owner executes `gokrax spec continue` (or automatically when auto_continue is set).

**Send prompt (send_to_agent):**
```
The following spec has been approved. Please propose an Issue breakdown for implementation.

Spec: {spec_path} (rev{final_rev})
Project: {project}

## Proposal Guidelines
- Granularity where CC (Claude Code) can implement 1 Issue = 1 MR (1–3 files / 100–500 lines)
- Explicitly state dependencies (DAG)
- Phase breakdown (groups that can be worked on in parallel)
- For each Issue: title, files to change, estimated lines, spec reference sections

## Output Format
```yaml
phases:
  - name: "Phase 1: Foundation"
    issues:
      - title: "config.py: spec mode foundation"
        files: ["config.py", "pipeline_io.py"]
        lines: 110
        spec_refs: ["§3.1", "§3.2"]
        depends_on: []
```
```

Collection is stored in `spec_config.issue_suggestions`. Per-reviewer timeout (SPEC_ISSUE_SUGGESTION_TIMEOUT_SEC).

---

## 8. ISSUE_PLAN Phase

### 8.1 Issue Merging and Creation

Request sent to spec_implementer via `send_to_agent()`:
```
Merge the reviewer Issue breakdown proposals and create GitLab Issues.

Project: {project} (GitLab)
Spec: {spec_path} (rev{final_rev})

## Reviewer Proposals
{issue_suggestions_formatted}

## Creation Rules
1. Merge proposals and determine the final Issue list
2. Prefix each Issue title with [spec:{spec_name}:S-{N}]
3. Check for duplicates before creation: `glab issue list --search "[spec:{spec_name}]"`
4. Create with `glab issue create`
5. Report created numbers (recorded incrementally in created_issues[])
6. Include ⚠️ annotation at the end of Issue body

After creation, report in YAML:
```yaml
status: done
created_issues: [51, 52, 53]
```
```

Created Issue numbers are incrementally recorded in `created_issues[]` and skipped on retry.

### 8.2 Annotation Existence Check

After creation, read back with `glab issue show` and verify ⚠️ annotation. If missing, auto-append with `glab issue note`.

---

## 9. QUEUE_PLAN Phase

Request sent to spec_implementer via `send_to_agent()`:
```
Generate batch lines for gokrax-queue.txt from the created Issues.

Project: {project}
Created Issues: {created_issues}
Spec: {spec_path}

## Generation Rules
1. Analyze dependencies between Issues
2. Issues that can run in parallel go in the same batch
3. Format: `{project} {issue_nums} full [--keep-context] # Reason`
4. Append generated lines to {queue_file_path}

Report in YAML when done:
```yaml
status: done
batches: 5
queue_file: "gokrax-queue.txt"
```
```

Appends to `config.QUEUE_FILE`. On completion → SPEC_DONE. Owner runs `gokrax spec done` to go to IDLE.

---

## 10. Watchdog Integration

### 10.1 watchdog.py Extension

```python
@dataclass
class SpecTransitionAction:
    next_state: str | None = None
    expected_state: str | None = None   # For DCL: current state (used for conflict detection)
    send_to: dict[str, str] | None = None  # {agent_id: message}
    discord_notify: str | None = None   # Discord notification text
    pipeline_updates: dict | None = None  # Update diff for spec_config
    error: str | None = None
    nudge_reviewers: list[str] | None = None   # List of reviewers to nudge
    nudge_implementer: bool = False              # Implementer nudge flag

def check_transition_spec(
    state: str,
    spec_config: dict,
    now: datetime,
) -> SpecTransitionAction:
    """Pure function. No side effects."""
    if state not in SPEC_STATES:
        return SpecTransitionAction(
            next_state="SPEC_PAUSED",
            error=f"Unknown spec state: {state}",
            discord_notify=f"[Spec] ⚠️ Unknown state {state} → SPEC_PAUSED",
            pipeline_updates={"paused_from": state},
        )

    if state == "SPEC_REVIEW":
        return _check_spec_review(spec_config, now)
    elif state == "SPEC_REVISE":
        return _check_spec_revise(spec_config, now)
    elif state == "SPEC_APPROVED":
        if spec_config.get("review_only"):
            return SpecTransitionAction(next_state="SPEC_DONE",
                discord_notify=f"[Spec] spec approved (--review-only)")
        if spec_config.get("auto_continue"):
            return SpecTransitionAction(next_state="ISSUE_SUGGESTION",
                discord_notify=f"[Spec] spec approved → auto-proceeding to Issue breakdown")
        # Default: awaiting Owner confirmation. Notification already fired at transition source (_check_spec_review approved branch)
        return SpecTransitionAction(next_state=None)
    elif state == "ISSUE_SUGGESTION":
        return _check_issue_suggestion(spec_config, now)
    elif state == "ISSUE_PLAN":
        return _check_issue_plan(spec_config, now)
    elif state == "QUEUE_PLAN":
        return _check_queue_plan(spec_config, now)
    elif state in ("SPEC_DONE", "SPEC_STALLED", "SPEC_REVIEW_FAILED", "SPEC_PAUSED"):
        return SpecTransitionAction(next_state=None)  # Awaiting Owner action
```

<!-- [v4] Dijkstra M-3: SPEC_APPROVED notification fires at transition source -->
**Notification firing rule:** Notifications are returned within the action that executes the state transition. Notifications are not sent during watchdog ticks while staying in a state. Examples:
- _check_spec_review() determines "approved" → returns SpecTransitionAction with `discord_notify="[Spec] spec approved (rev{N})"`
- check_transition_spec during SPEC_APPROVED stay → `discord_notify=None`

<!-- [v4] Leibniz C-2 / Dijkstra C-1: update_pipeline pattern -->
**Integration in process() (DCL pattern):**
```python
def _apply_spec_action(pipeline_path: str, action: SpecTransitionAction, now: datetime):
    """Uses the existing update_pipeline() pattern. Reloads from disk + verifies state match."""
    applied = False
    applied_action = None

    def _update(data):
        nonlocal applied, applied_action
        # [v5] Check expected_state match only. Always trust recomputed result
        if data["state"] != action.expected_state:
            return  # Conflict: another process already transitioned
        sc = data.get("spec_config", {})
        action2 = check_transition_spec(data["state"], sc, now)
        # State transition (if next_state is present)
        if action2.next_state:
            data["state"] = action2.next_state
        # pipeline_updates always applied (even when next_state=None)
        if action2.pipeline_updates:
            data["spec_config"].update(action2.pipeline_updates)
        if action2.next_state or action2.pipeline_updates or action2.send_to or action2.discord_notify:
            applied = True
            applied_action = action2

    update_pipeline(pipeline_path, _update)

    # Side effects only if applied (using action2's results)
    if applied and applied_action:
        if applied_action.send_to:
            for agent_id, msg in applied_action.send_to.items():
                send_to_agent(agent_id, msg)
        if applied_action.discord_notify:
            notify_discord(applied_action.discord_notify)
```

### 10.2 Timeouts and Nudges

| State | Timeout | After timeout | MAX_RETRIES exceeded |
|---|---|---|---|
| SPEC_REVIEW | 1800s/reviewer | Decide with received only | N/A (per-reviewer) |
| SPEC_REVISE | 1800s | retry_counts[REVISE]++ & resend | PAUSED |
| ISSUE_SUGGESTION | 600s/reviewer | Transition with received only | N/A (per-reviewer) |
| ISSUE_PLAN | 1800s | retry_counts[PLAN]++ & resend | PAUSED |
| QUEUE_PLAN | 1800s | retry_counts[QUEUE]++ & resend | PAUSED |

---

## 11. notify.py Extension

Bullet-point based. Split when exceeding 2000 characters.

**Notifications on state transition (sent once within the transition action):**
- → SPEC_REVIEW: `[Spec] {project}: rev{N} review started ({reviewer_count} reviewers)`
- → SPEC_REVISE: `[Spec] {project}: rev{N} review completed — C:{n} M:{n} m:{n} s:{n}`
- → SPEC_APPROVED: `[Spec] {project}: spec approved (rev{N}). Run \`gokrax spec continue\` to proceed to Issue breakdown`
- → SPEC_APPROVED (forced): `[Spec] ⚠️ {project}: force approved (P1+ {n} items remaining)`
- → SPEC_STALLED: `[Spec] ⏸️ {project}: MAX_CYCLES reached, P1+ {n} items remaining`
- → SPEC_REVIEW_FAILED: `[Spec] ❌ {project}: insufficient valid reviews`
- → SPEC_PAUSED: `[Spec] ⏸️ {project}: pipeline paused — {reason}`
- → ISSUE_PLAN complete: `[Spec] {project}: {n} issues created`
- → QUEUE_PLAN complete: `[Spec] {project}: {n} batches queued`
- → SPEC_DONE: `[Spec] ✅ {project}: spec mode completed`

**REVISE completion notifications:**
- With commit hash: `[Spec] {project}: rev{N} revision completed ({commit[:7]})` (truncated to first 7 characters)
- Git commit failed (empty commit): `[Spec] ⚠️ {project}: rev{N} git commit failed` → SPEC_PAUSED
- No changes (zero diff): `[Spec] ⚠️ {project}: rev{N} no changes (empty revision)` → SPEC_PAUSED

**Failure notifications:** YAML parse failure, send failure, git push failure, glab issue creation failure

---

## 12. Review Result Storage

### 12.1 File Storage

<!-- [v4] Leibniz M-2: pipelines_dir specification -->

**Raw review files (pipelines_dir):**
- Path: `PIPELINES_DIR/{project}/spec-reviews/` (recorded as absolute path in pipeline.json)
- Retention period: 30 days (`SPEC_REVIEW_RAW_RETENTION_DAYS`). Watchdog deletes expired files on SPEC_DONE transition
- <!-- [v6] Leibniz C-1 --> Permissions: directory=0700 (owner rwx), file=0600 (owner rw). Set by watchdog via chmod on directory creation
- Filename: `{YYYYMMDD}T{HHMMSS}_{reviewer}_{spec_name}_rev{N}.md`

**Review summary (in repo):**
- Path: `reviews/` directory
- Filename: `{YYYYMMDD}T{HHMMSS}_merged_{spec_name}_rev{N}.md`
- Commit author: gokrax (via watchdog)
- Message: `[spec-review] {project}: rev{N} reviews ({reviewer_count} reviewers)`
- Timing: Immediately before transitioning to SPEC_REVISE

### 12.2 review_history

```json
{
  "rev": "1", "rev_index": 1,
  "reviews": {"reviewer1": {"verdict": "P0", "counts": {...}}, ...},
  "merged_counts": {"critical": 18, "major": 14, "minor": 14, "suggestion": 8},
  "commit": "82ec516",
  "timestamp": "2026-02-28T21:15:00+09:00"
}
```

---

## 13. Implementation Results Summary

All spec mode features are implemented. Below is the actual file structure and overview.

### 13.1 Implementation Files

| File | Contents |
|---|---|
| `config/states.py` | SPEC_STATES, SPEC_TRANSITIONS, constants (MAX_SPEC_REVISE_CYCLES, MIN_VALID_REVIEWS_BY_MODE, timeout constants, etc.) |
| `commands/spec.py` | spec CLI 14 commands: start, stop, approve, continue, done, retry, resume, extend, status, review-submit, revise-submit, self-review-submit, issue-submit, queue-submit, suggestion-submit |
| `engine/fsm_spec.py` | check_transition_spec(), _apply_spec_action(), state-specific decision functions |
| `spec_review.py` | parse_review_yaml(), should_continue_review(), merge_reviews(), build_review_history_entry(), format_merged_report() |
| `spec_revise.py` | parse_revise_response(), parse_self_review_response(), extract_rev_from_path() |
| `spec_issue.py` | build_issue_suggestion_prompt(), parse_issue_suggestion_response(), build_issue_plan_prompt(), parse_issue_plan_response(), build_queue_plan_prompt(), parse_queue_plan_response() |
| `pipeline_io.py` | default_spec_config(), validate_spec_config(), check_spec_mode_exclusive(), ensure_spec_reviews_dir() |
| `notify.py` | spec_notify_* function family (16 functions: review_start, review_complete, approved, approved_auto, approved_forced, stalled, review_failed, paused, revise_done, revise_commit_failed, revise_no_changes, issue_plan_done, queue_plan_done, done, failure, self_review_failed) |
| `messages/ja/spec/` | Review, revision, approval, Issue, etc. prompt templates |
| `tests/` | spec mode related test suite |

### 13.2 Deviations from Original Plan

- CLI command count: planned 8 → implemented 14 (6 submit commands added)
- CLI definitions separated from `gokrax.py` into `commands/spec.py`
- State transition logic separated from `watchdog.py` into `engine/fsm_spec.py`
- notify.py spec notification functions: planned ~10 → implemented 16

---

## 14. Future Extensions

- Automated spec draft generation
- Differential review (token savings)
- Embedding similarity-based duplicate candidate suggestions
- SPEC_PAUSED auto-recovery (transient errors)

---

## Appendix A: Historical Changelogs

<details>
<summary>All v1→v2 changes (42 items) — click to expand</summary>

- [v2] Fix auto-approval bug on empty review set (Pascal C-1)
- [v2] Remove forced approval on MAX_CYCLES reached (Pascal C-2)
- [v2] Prevent infinite stack on timeout (Pascal C-3 / Leibniz C-10)
- [v2] Unify verdict/severity vocabulary (Pascal C-4 / Leibniz s-1)
- [v2] Initialize reviewer list on --skip-review (Pascal C-5 / Dijkstra M-6)
- [v2] Prevent review filename overwrite (Pascal C-6 / Dijkstra m-4)
- [v2] Separate from batch mechanism (Leibniz C-1 / Dijkstra C-2)
- [v2] Fix send interface (Leibniz C-2)
- [v2] Register SPEC_* in VALID_STATES/VALID_TRANSITIONS (Leibniz C-3 / Dijkstra C-3)
- [v2] Fix should_continue_review pseudocode (Leibniz C-4)
- [v2] Unify spec_config JSON example and field table (Leibniz C-5 / Dijkstra C-1)
- [v2] Clean up state sets (Leibniz C-6)
- [v2] Resolve QUEUE_FILE double definition (Leibniz C-7 / Dijkstra m-2)
- [v2] Ensure YAML parse determinism (Leibniz C-8 / Dijkstra C-5)
- [v2] Safe deduplication merging (Leibniz C-9)
- [v2] Per-reviewer timeout data structure (Leibniz C-10 / Dijkstra M-3)
- [v2] Review storage location convention (Leibniz C-11)
- [v2] Unify rev naming convention (Leibniz M-1 / Dijkstra s-3)
- [v2] Add CLI→pipeline mapping table (Leibniz M-2 / Dijkstra m-8)
- [v2] Define diff info generation method (Leibniz M-3)
- [v2] Normalize review item IDs (Leibniz M-4)
- [v2] Manual approve audit log (Leibniz M-5)
- [v2] Make check_spec_mode a pure function (Leibniz M-6 / Dijkstra C-3)
- [v2] Handle REJECT verdict (Dijkstra M-1)
- [v2] YAML code block nesting workaround (Dijkstra M-2)
- [v2] Define ISSUE_SUGGESTION data flow (Dijkstra M-4)
- [v2] SPEC_DONE→IDLE transition command (Dijkstra M-5)
- [v2] Partial failure recovery for GitLab Issue creation (Dijkstra M-7)
- [v2] Improve self-review (Dijkstra M-8)
- [v2] Fix §6.3 section number duplication (Leibniz m-1 / Dijkstra C-4)
- [v2] Handle Discord notification character limit (Leibniz m-2)
- [v2] Remove full spec embedding (Leibniz m-3 / Dijkstra m-6 / Owner directive)
- [v2] Issue annotation existence check (Leibniz m-4)
- [v2] Add Owner confirmation gate (Dijkstra m-5)
- [v2] Add failure notifications (Leibniz s-2)
- [v2] DAG fix: S-6 dependency target (Dijkstra m-7)
- [v2] Fix §1.2 scope (Dijkstra s-1)
- [v2] Add MergedReviewReport type definition (Dijkstra s-2)
- [v2] Fix S-4 line count estimate (Dijkstra s-4)
- [v2] Add early termination options truth table (Dijkstra s-5)
- [v2] Error handling policy (Dijkstra s-6)
- [v2] Simplify deduplication algorithm (Dijkstra m-3 / Owner directive)
- [v2] Handle empty commit in REVISE completion notification (Dijkstra m-9)

</details>

<details>
<summary>All v5→v6 changes (7 items) — click to expand</summary>

- [v6] Fix pipelines_dir permissions (Leibniz C-1)
- [v6] Add discord_notify to guard condition (Leibniz C-2)
- [v6] Add last_changes field (Pascal P-1)
- [v6] Recalculate timeout on ISSUE_SUGGESTION resume (Pascal P-2)
- [v6] Add reviewed_rev to current_reviews (Leibniz M-1)
- [v6] Add status field to current_reviews (Leibniz m-1)
- [v6] Fix §4.1 extend description (Dijkstra s-2)

</details>

<details>
<summary>All v4→v5 changes (8 items) — click to expand</summary>

- [v5] Relax DCL application condition (Leibniz C-1 / Pascal P-3 / Dijkstra m-2)
- [v5] Apply next_state=None actions (Dijkstra M-1)
- [v5] extend→SPEC_REVISE direct (Pascal P-1)
- [v5] Clarify timeout_at reset responsibility (Leibniz C-2)
- [v5] Add timeout entries to current_reviews (Leibniz M-1)
- [v5] Archive current_reviews on approve --force (Pascal P-2)
- [v5] Unify pipelines_dir path notation (Leibniz m-1)
- [v5] Specify expired file deletion executor (Dijkstra m-1)

</details>

<details>
<summary>All v3→v4 changes (15 items) — click to expand</summary>

- [v4] Simplify SPEC_REVISE flow (Leibniz C-1 / Pascal C-1 / Dijkstra C-2)
- [v4] DCL reload (Leibniz C-2 / Dijkstra C-1)
- [v4] Merge order determinism (Leibniz C-3)
- [v4] Initialize review_requests on extend/resume (Pascal C-2 / Dijkstra m-2)
- [v4] PAUSED after self-review retry limit (Pascal M-1 / Dijkstra M-2)
- [v4] Stricter paused determination (Pascal M-2)
- [v4] Single source of truth for decision logic (Leibniz M-1)
- [v4] Specify pipelines_dir (Leibniz M-2)
- [v4] CLI option precedence table (Leibniz m-1)
- [v4] Clarify SPEC_APPROVED notification firing source (Dijkstra M-3)
- [v4] §6.3 step consolidation (Dijkstra M-1)
- [v4] review_requests reset: on REVIEW entry (Dijkstra m-2)
- [v4] --review-only + --auto-continue → review-only wins (Dijkstra m-3)
- [v4] Add SpecTransitionAction.expected_state field (self-check)
- [v4] Prevent spurious notifications on _apply_spec_action conflict (self-check)

</details>

<details>
<summary>All v2→v3 changes (27 items) — click to expand</summary>

- [v3] Fix VALID_TRANSITIONS overwrite bug (Dijkstra C-1)
- [v3] Watchdog early return on empty batch (Leibniz C-1)
- [v3] Clean up check_transition_spec I/O (Pascal P-2 / Leibniz C-3 / Dijkstra C-2)
- [v3] Resolve transition contradiction when all parse failed (Pascal P-1)
- [v3] Prevent P1 loop runaway (Leibniz C-4)
- [v3] Handle revise_count on STALLED→REVIEW (Leibniz C-5)
- [v3] Stricter retry_count granularity (Leibniz C-6)
- [v3] Prevent instant timeout death on resume (Pascal P-3)
- [v3] Issue creation race condition mitigation (Pascal P-4)
- [v3] PAUSED transition target completeness (Pascal P-5)
- [v3] Specify revise_count increment (Pascal P-6 / Dijkstra C-3)
- [v3] Stricter VERDICT_ALIASES (Leibniz M-1 / Dijkstra m-2)
- [v3] reviews/ commit convention (Leibniz M-2)
- [v3] Structured approve --force audit log (Leibniz M-3)
- [v3] Detailed retry/resume command spec (Dijkstra M-1)
- [v3] PAUSED return validation (Dijkstra M-2)
- [v3] Unify paths in prompts (Dijkstra M-3)
- [v3] Self-review Pass 2 details (Dijkstra M-4)
- [v3] Restore ISSUE prompts (Dijkstra M-5)
- [v3] Specify single YAML block constraint (Leibniz m-1)
- [v3] STALLED→REVIEW CLI definition (Dijkstra m-1)
- [v3] Implement check_transition_spec for all states (Dijkstra m-3)
- [v3] Distinguish empty commit (Dijkstra m-5)
- [v3] Separate changelog (Dijkstra s-2)
- [v3] Rename --no-issue → --review-only (Owner directive)
- [v3] Add --auto-continue flag (Owner directive)
- [v3] Implicit auto_continue=true on --skip-review (Owner directive)

</details>
