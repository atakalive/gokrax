# gokrax — Architecture & State Machine Diagrams

> Last updated: 2026-03-29

## 1. System Architecture (Overall Flow)

```mermaid
graph LR
    subgraph Input
        GL[("GitLab<br/>Issues")]
        Owner["Human<br/>Operator"]
    end

    subgraph gokrax["gokrax (Pipeline Orchestrator)"]
        CLI["gokrax CLI"]
        SM["State Machine<br/>(pipeline.json)"]
        WD["Watchdog<br/>(polling loop)"]
        TQ["Task Queue<br/>(batch execution)"]
    end

    subgraph Execution
        BD["Backend Dispatch<br/>(engine/backend.py)"]
        BE_OC["openclaw Backend<br/>(engine/backend_openclaw.py)"]
        BE_PI["pi Backend<br/>(engine/backend_pi.py)"]
        BE_CC["cc Backend<br/>(engine/backend_cc.py)"]
        BE_GM["gemini Backend<br/>(engine/backend_gemini.py)"]
        BE_KM["kimi Backend<br/>(engine/backend_kimi.py)"]
        CC1["Claude Code CLI<br/>(Impl Lead 1)"]
        CC2["Claude Code CLI<br/>(Impl Lead 2)"]
        BD --> BE_OC
        BD --> BE_PI
        BD --> BE_CC
        BD --> BE_GM
        BD --> BE_KM
        BE_CC --> CC1
        BE_CC --> CC2
    end

    subgraph Review["Reviewer Ensemble"]
        direction TB
        R_REG["Regular Tier<br/>Reviewer A · Reviewer B · Reviewer C"]
        R_SHORT["Short-context Tier<br/>Reviewer D · Reviewer E · Reviewer F"]
    end

    subgraph Output
        MR[("GitLab<br/>Merge Request")]
        DC["Discord<br/>Notifications"]
        WB["WatcherB<br/>(GUI Monitor)"]
    end

    GL -->|"issue created"| CLI
    Owner -->|"gokrax start"| CLI
    CLI --> SM
    SM <--> WD
    WD -->|"dispatch task"| TQ
    TQ -->|"design/implement"| BD
    BD -->|"design/implement"| CC1
    BD -->|"design/implement"| CC2
    WD -->|"request review"| R_REG
    WD -->|"request review"| R_SHORT
    R_REG -->|"verdict"| CLI
    R_SHORT -->|"verdict"| CLI
    CC1 -->|"code complete"| CLI
    CC2 -->|"code complete"| CLI
    CLI -->|"merge"| MR
    WD -->|"status updates"| DC
    DC -.->|"webhook"| WB
```

## 2. Pipeline State Machine (Main Flow)

```mermaid
stateDiagram-v2
    [*] --> IDLE

    IDLE --> INITIALIZE : gokrax start

    INITIALIZE --> DESIGN_PLAN : auto-transition
    INITIALIZE --> DESIGN_APPROVED : skip_design

    state "Design Phase" as design {
        DESIGN_PLAN --> DESIGN_REVIEW : plan submitted
        DESIGN_REVIEW --> DESIGN_APPROVED : all reviewers APPROVE
        DESIGN_REVIEW --> DESIGN_REVISE : P0/P1/REJECT verdict
        DESIGN_REVISE --> DESIGN_REVIEW : revision submitted
        DESIGN_REVIEW --> BLOCKED : timeout / stall
        DESIGN_REVIEW --> DESIGN_REVIEW_NPASS : n_pass > 1 reviewers exist
        DESIGN_REVIEW_NPASS --> DESIGN_APPROVED : all passes complete + APPROVE
        DESIGN_REVIEW_NPASS --> DESIGN_REVISE : P0/P1 verdict
        DESIGN_REVIEW_NPASS --> DESIGN_REVIEW_NPASS : more passes remaining
        DESIGN_REVIEW_NPASS --> DESIGN_APPROVED : timeout (verdict-dependent)
        DESIGN_REVIEW_NPASS --> DESIGN_REVISE : timeout + P0/P1
    }

    state "Implementation Phase" as impl {
        IMPLEMENTATION --> CODE_TEST : code submitted
        IMPLEMENTATION --> CODE_REVIEW : code submitted (skip test)
        CODE_TEST --> CODE_REVIEW : tests pass
        CODE_TEST --> CODE_TEST_FIX : tests fail
        CODE_TEST --> BLOCKED : timeout / stall
        CODE_TEST_FIX --> CODE_TEST : fix submitted
        CODE_TEST_FIX --> BLOCKED : timeout / stall
        CODE_REVIEW --> CODE_APPROVED : all reviewers APPROVE
        CODE_REVIEW --> CODE_REVISE : P0/P1/REJECT verdict
        CODE_REVISE --> CODE_TEST : revision submitted (re-test)
        CODE_REVISE --> CODE_REVIEW : revision submitted (re-review)
        CODE_REVIEW --> BLOCKED : timeout / stall
        CODE_REVIEW --> CODE_REVIEW_NPASS : n_pass > 1 reviewers exist
        CODE_REVIEW_NPASS --> CODE_APPROVED : all passes complete + APPROVE
        CODE_REVIEW_NPASS --> CODE_REVISE : P0/P1 verdict
        CODE_REVIEW_NPASS --> CODE_REVIEW_NPASS : more passes remaining
        CODE_REVIEW_NPASS --> CODE_APPROVED : timeout (verdict-dependent)
        CODE_REVIEW_NPASS --> CODE_REVISE : timeout + P0/P1
    }

    DESIGN_APPROVED --> ASSESSMENT : auto-transition
    DESIGN_APPROVED --> IMPLEMENTATION : skip_assess
    ASSESSMENT --> IMPLEMENTATION : assessed (Lvl 1-5)
    ASSESSMENT --> IDLE : domain_risk exclusion

    CODE_APPROVED --> MERGE_SUMMARY_SENT : auto-transition
    MERGE_SUMMARY_SENT --> DONE : human approves merge / automerge / gokrax ok

    DONE --> IDLE : next issue
    BLOCKED --> IDLE : manual reset

    note right of INITIALIZE : skip_design → DESIGN_APPROVED
    note right of DESIGN_APPROVED : skip_assess → IMPLEMENTATION
    note right of DESIGN_REVISE : MAX_REVISE_CYCLES = 4
    note right of CODE_REVISE : MAX_REVISE_CYCLES = 4
```

### VALID_TRANSITIONS (reference)

| From | To |
|------|----|
| IDLE | INITIALIZE |
| INITIALIZE | DESIGN_PLAN, DESIGN_APPROVED |
| DESIGN_PLAN | DESIGN_REVIEW |
| DESIGN_REVIEW | DESIGN_APPROVED, DESIGN_REVISE, BLOCKED, DESIGN_REVIEW_NPASS |
| DESIGN_REVIEW_NPASS | DESIGN_APPROVED, DESIGN_REVISE, DESIGN_REVIEW_NPASS |
| DESIGN_REVISE | DESIGN_REVIEW |
| DESIGN_APPROVED | ASSESSMENT, IMPLEMENTATION |
| ASSESSMENT | IMPLEMENTATION, IDLE |
| IMPLEMENTATION | CODE_TEST, CODE_REVIEW |
| CODE_TEST | CODE_REVIEW, CODE_TEST_FIX, BLOCKED |
| CODE_TEST_FIX | CODE_TEST, BLOCKED |
| CODE_REVIEW | CODE_APPROVED, CODE_REVISE, BLOCKED, CODE_REVIEW_NPASS |
| CODE_REVIEW_NPASS | CODE_APPROVED, CODE_REVISE, CODE_REVIEW_NPASS |
| CODE_REVISE | CODE_TEST, CODE_REVIEW |
| CODE_APPROVED | MERGE_SUMMARY_SENT |
| MERGE_SUMMARY_SENT | DONE |
| DONE | IDLE |
| BLOCKED | IDLE |

## 3. Review Ensemble Detail

```mermaid
graph TB
    subgraph "Review Modes (per-project configurable, defined in settings.py)"
        FULL["<b>full</b> (4 reviewers)<br/>members: [...]"]
        STD["<b>standard</b> (3 reviewers)<br/>members: [...]"]
        LITE["<b>lite</b> (2 reviewers)<br/>members: [...]"]
        MIN["<b>min</b> (1 reviewer)<br/>members: [...]"]
        SKIP["<b>skip</b> (0 reviewers)<br/>auto-approve"]
    end

    subgraph "Reviewer Tiers (defined in settings.py)"
        REG["<b>Regular</b><br/>members: [...]"]
        FREE["<b>Free</b><br/>(empty — no current assignment)"]
        SHORT["<b>Short-context</b><br/>members: [...]"]
    end

    subgraph "Dispatch Logic"
        D1["1. Send /new to all members"]
        D2["2. Wait for responses"]
        D3["3. Collect until min_reviews met"]
        D4["4. Aggregate verdicts<br/>   (worst severity wins)"]
    end

    D1 --> D2 --> D3 --> D4
```

### Review Modes Table

Review modes are defined in `settings.py` (`REVIEW_MODES`). See `settings.example.py` for defaults.

| Mode | Members | min_reviews | grace_period_sec | n_pass |
|------|---------|-------------|------------------|--------|
| full | Defined in `settings.py` `REVIEW_MODES` | 4 | 0 | — |
| standard | Defined in `settings.py` `REVIEW_MODES` | 3 | 0 | — |
| lite | Defined in `settings.py` `REVIEW_MODES` | 2 | 0 | — |
| min | Defined in `settings.py` `REVIEW_MODES` | 1 | 0 | — |
| skip | (none) | 0 | 0 | — |
| standard-x2 | Defined in `settings.py` `REVIEW_MODES` | 3 | 0 | {reviewer1: 2, reviewer3: 2} |

### Phase Override

Review modes support per-phase (design/code) configuration overrides.
Fields not overridden inherit from the mode's top-level defaults.

Example in `settings.py`:
```python
"full-custom": {
    "members": ["reviewer1", "reviewer2", "reviewer3", "reviewer4"],
    "code": {
        "members": ["reviewer1", "reviewer2", "reviewer3"],
        "n_pass": {"reviewer1": 2},
    },
}
```

### Reviewer Tiers

Reviewer tiers are defined in `settings.py` (`REVIEWER_TIERS`). See `settings.example.py` for defaults.

| Tier | Members |
|------|---------|
| Regular | [] |
| Free | [] |
| Short-context | [] |

### N-Pass Review

N-pass review allows specified reviewers to perform multiple review passes on the same code/design.

#### Configuration

Add `n_pass` to a review mode in `settings.py`:

```python
"standard-x2": {
    "members": [],
    "min_reviews": 3,
    "n_pass": {"reviewer1": 2, "reviewer3": 2},
}
```

Reviewers not listed in `n_pass` default to 1 pass.

#### Flow

1. Pass 1 completes normally in DESIGN_REVIEW / CODE_REVIEW
2. If any reviewer has `n_pass > 1`, transitions to *_REVIEW_NPASS
3. NPASS reviewers receive a lightweight prompt (no issue body/diff re-send)
4. When all NPASS passes complete, final verdict uses `count_reviews()` — counts each reviewer's latest verdict as one vote (n_pass=1 reviewers included)
5. P0/P1 from any submitted reviewer → immediate REVISE (no timeout wait needed)
6. After REVISE → REVIEW, pass counters reset; pass 1 starts over (does not re-enter NPASS directly)

#### GitLab Note Behavior in Intermediate Passes

- APPROVE in intermediate pass (pass < target_pass): GitLab note is **skipped**
- P0/P1/P2 in intermediate pass: GitLab note is **posted** (so developers can see the feedback)

#### Timeout

- NPASS uses the same timeout as the base REVIEW state
- On timeout: `count_reviews()` collects all current verdicts (incomplete NPASS reviewers retain their pass 1 verdict) and `_resolve_review_outcome` determines the transition. P0/P1 → REVISE even on timeout
- NPASS does **not** transition to BLOCKED

#### Forced Externalization

- Triggered at CODE_REVIEW state entry (inside `notify_reviewers`), not at queue submission
- When `n_pass > 1` reviewers exist in the review mode, CODE_REVIEW always externalizes review data to a file, regardless of message size
- This ensures NPASS prompts can reference the file path
- Existing queued batches are unaffected until they enter CODE_REVIEW

## 4. Watchdog Cycle

```mermaid
graph TD
    START["Watchdog Loop<br/>(20s interval)"] --> DISCORD["Check Discord<br/>commands"]
    DISCORD --> QUEUE["Check queue<br/>(auto-pop)"]
    QUEUE --> SCAN["Scan all pipeline.json files"]
    SCAN --> CHECK{"Active issue<br/>found?"}
    CHECK -->|No| IDLE_CHECK{"All projects<br/>DONE/IDLE?"}
    IDLE_CHECK -->|Yes| STOP["Auto-stop<br/>watchdog loop"]
    IDLE_CHECK -->|No| WAIT["Sleep 20s"]
    CHECK -->|Yes| TIMEOUT{"Timed out?"}
    TIMEOUT -->|Yes| NUDGE["Send nudge /<br/>auto-transition<br/>to BLOCKED"]
    TIMEOUT -->|No| PENDING{"Pending<br/>notification?"}
    PENDING -->|Yes| NOTIFY["Send Discord<br/>notification"]
    PENDING -->|No| WAIT
    NUDGE --> WAIT
    NOTIFY --> WAIT
    WAIT --> SCAN
```

## 5. End-to-End Issue Lifecycle (Sequence)

```mermaid
sequenceDiagram
    participant Owner as Human Operator
    participant DB as gokrax CLI
    participant WD as Watchdog
    participant CC as Claude Code
    participant RV as Reviewers
    participant GL as GitLab
    participant DC as Discord

    Owner->>DB: gokrax start --project X --issue 42
    DB->>DB: Set state -> INITIALIZE
    DB->>DB: Auto -> DESIGN_PLAN
    DB->>DC: Plan started
    WD->>CC: /new (design plan task)
    CC->>DB: gokrax plan-done
    DB->>DB: Set state -> DESIGN_REVIEW
    DB->>DC: Review requested

    par Review Ensemble
        WD->>RV: /new (review task) x N reviewers
        RV->>DB: gokrax review --verdict APPROVE
    end

    DB->>DB: Set state -> DESIGN_APPROVED
    DB->>DB: Auto -> ASSESSMENT
    WD->>CC: Assess complexity (Lvl 1-5)
    CC->>DB: gokrax assess-done --complex-level N
    DB->>DB: Auto -> IMPLEMENTATION
    DB->>DC: Implementation started
    WD->>CC: /new (implement task)
    CC->>GL: git push (branch)
    CC->>DB: gokrax commit --hash <hash>
    DB->>DB: Set state -> CODE_TEST
    DB->>DC: Tests running
    WD->>CC: /new (test task)
    WD->>DB: test result (pass) → CODE_REVIEW
    DB->>DC: Code review requested

    par Review Ensemble
        WD->>RV: /new (review task) x N reviewers
        RV->>DB: gokrax review --verdict APPROVE
    end

    DB->>DB: Set state -> CODE_APPROVED
    DB->>DB: Auto -> MERGE_SUMMARY_SENT
    DB->>DC: Merge summary
    alt Merge approval
        Owner->>DB: "OK" (Discord reply)
    else automerge enabled
        DB->>DB: automerge flag → DONE
    else CLI approval
        Owner->>DB: gokrax ok --pj X
    end
    DB->>GL: glab mr merge
    DB->>DB: Set state -> DONE
    DB->>DC: Issue complete
```

## 6. Spec Mode State Machine

Spec mode manages the specification review cycle, separate from the main pipeline flow.
Entry point: `gokrax spec start` transitions from IDLE → SPEC_REVIEW (with review) or IDLE → SPEC_APPROVED (review skipped).

### Spec States

SPEC_REVIEW, SPEC_REVISE, SPEC_APPROVED, ISSUE_SUGGESTION, ISSUE_PLAN, QUEUE_PLAN, SPEC_DONE, SPEC_STALLED, SPEC_REVIEW_FAILED, SPEC_PAUSED

### SPEC_TRANSITIONS (reference)

| From | To |
|------|----|
| IDLE | SPEC_REVIEW, SPEC_APPROVED |
| SPEC_REVIEW | SPEC_REVISE, SPEC_APPROVED, SPEC_STALLED, SPEC_REVIEW_FAILED, SPEC_PAUSED |
| SPEC_REVISE | SPEC_REVIEW, SPEC_PAUSED |
| SPEC_APPROVED | ISSUE_SUGGESTION, SPEC_DONE |
| ISSUE_SUGGESTION | ISSUE_PLAN, SPEC_PAUSED |
| ISSUE_PLAN | QUEUE_PLAN, SPEC_DONE, SPEC_PAUSED |
| QUEUE_PLAN | SPEC_DONE, SPEC_PAUSED |
| SPEC_DONE | IDLE |
| SPEC_STALLED | SPEC_APPROVED, SPEC_REVISE |
| SPEC_REVIEW_FAILED | SPEC_REVIEW |
| SPEC_PAUSED | SPEC_REVIEW, SPEC_REVISE, SPEC_APPROVED, ISSUE_SUGGESTION, ISSUE_PLAN, QUEUE_PLAN, SPEC_DONE |

```mermaid
stateDiagram-v2
    [*] --> IDLE

    IDLE --> SPEC_REVIEW : gokrax spec start (with review)
    IDLE --> SPEC_APPROVED : gokrax spec start (skip review)

    SPEC_REVIEW --> SPEC_REVISE : P0/P1/REJECT verdict
    SPEC_REVIEW --> SPEC_APPROVED : all reviewers APPROVE
    SPEC_REVIEW --> SPEC_STALLED : timeout / stall
    SPEC_REVIEW --> SPEC_REVIEW_FAILED : review error
    SPEC_REVIEW --> SPEC_PAUSED : manual pause

    SPEC_REVISE --> SPEC_REVIEW : revision submitted
    SPEC_REVISE --> SPEC_PAUSED : manual pause

    SPEC_APPROVED --> ISSUE_SUGGESTION : auto-transition
    SPEC_APPROVED --> SPEC_DONE : no issues to suggest

    ISSUE_SUGGESTION --> ISSUE_PLAN : suggestion accepted
    ISSUE_SUGGESTION --> SPEC_PAUSED : manual pause

    ISSUE_PLAN --> QUEUE_PLAN : plan completed
    ISSUE_PLAN --> SPEC_DONE : done
    ISSUE_PLAN --> SPEC_PAUSED : manual pause

    QUEUE_PLAN --> SPEC_DONE : queue completed
    QUEUE_PLAN --> SPEC_PAUSED : manual pause

    SPEC_DONE --> IDLE : cycle complete

    SPEC_STALLED --> SPEC_APPROVED : manual override
    SPEC_STALLED --> SPEC_REVISE : retry

    SPEC_REVIEW_FAILED --> SPEC_REVIEW : retry

    note right of SPEC_PAUSED : SPEC_PAUSED can resume to\nany spec state (7 transitions)
```

## 7. Task Queue (Batch Execution)

### Queue File Format

Queue file: `gokrax-queue.txt` — one entry per line:

```
PROJECT ISSUES [MODE] [OPTIONS...]
```

### CLI Commands

| Command | Description |
|---------|-------------|
| `gokrax qrun` | Pop next queue entry and start |
| `gokrax qstatus` | Show queue + running batch status |
| `gokrax qadd` | Append entry to queue |
| `gokrax qdel` | Remove entry from queue |
| `gokrax qedit` | Edit entry in queue |

### Queue Options

Available options: `automerge`, `skip_cc_plan`, `no-cc`, `keep_ctx_intra`, `skip_test`, `skip_assess`, `skip_design`, `impl=MODEL`, `plan=MODEL`.
See `settings.example.py` `DEFAULT_QUEUE_OPTIONS` for defaults.

### Discord Integration

Watchdog's `check_discord_commands()` processes queue commands (`qrun`, `qstatus`, `qadd`, `qdel`, `qedit`) from Discord.

### QueueSkipError Retry

When `gokrax qrun` encounters a skippable condition (e.g., all issues closed), it exits with code 75 (`EXIT_QUEUE_SKIP`).

- `_check_queue()`: `while True` loop until a non-skip result. Each skipped entry is marked `# done:` by `pop_next_queue_entry`, so the loop always terminates (queue exhaustion → exit 0).
- `_handle_qrun()`: Same `while True` drain loop with batched Discord notifications.

Exit codes:
| Code | Meaning |
|------|---------|
| 0 | Success or queue empty |
| 75 | Entry skipped (next entry available) |
| other | Error (no retry) |
