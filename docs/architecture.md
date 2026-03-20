# gokrax — Architecture & State Machine Diagrams

> Last updated: 2026-03-20

## 1. System Architecture (Overall Flow)

```mermaid
graph LR
    subgraph Input
        GL[("GitLab<br/>Issues")]
        M["Human<br/>Operator"]
    end

    subgraph gokrax["gokrax (Pipeline Orchestrator)"]
        CLI["gokrax CLI"]
        SM["State Machine<br/>(pipeline.json)"]
        WD["Watchdog<br/>(polling loop)"]
        TQ["Task Queue<br/>(batch execution)"]
    end

    subgraph Execution
        CC1["Claude Code CLI<br/>(Impl Lead 1)"]
        CC2["Claude Code CLI<br/>(Impl Lead 2)"]
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
    M -->|"gokrax triage/run"| CLI
    CLI --> SM
    SM <--> WD
    WD -->|"dispatch task"| TQ
    TQ -->|"design/implement"| CC1
    TQ -->|"design/implement"| CC2
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

    TRIAGE --> IDLE : triage complete

    INITIALIZE --> DESIGN_PLAN : auto-transition

    state "Design Phase" as design {
        DESIGN_PLAN --> DESIGN_REVIEW : plan submitted
        DESIGN_REVIEW --> DESIGN_APPROVED : all reviewers APPROVE
        DESIGN_REVIEW --> DESIGN_REVISE : P0/P1/REJECT verdict
        DESIGN_REVISE --> DESIGN_REVIEW : revision submitted
        DESIGN_REVIEW --> BLOCKED : timeout / stall
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
    }

    DESIGN_APPROVED --> IMPLEMENTATION : auto-transition

    CODE_APPROVED --> MERGE_SUMMARY_SENT : auto-transition
    MERGE_SUMMARY_SENT --> DONE : human approves merge

    DONE --> IDLE : next issue
    BLOCKED --> IDLE : manual reset

    note right of DESIGN_REVISE : MAX_REVISE_CYCLES = 4
    note right of CODE_REVISE : MAX_REVISE_CYCLES = 4
```

### VALID_TRANSITIONS (reference)

| From | To |
|------|----|
| TRIAGE | IDLE |
| IDLE | INITIALIZE |
| INITIALIZE | DESIGN_PLAN |
| DESIGN_PLAN | DESIGN_REVIEW |
| DESIGN_REVIEW | DESIGN_APPROVED, DESIGN_REVISE, BLOCKED |
| DESIGN_REVISE | DESIGN_REVIEW |
| DESIGN_APPROVED | IMPLEMENTATION |
| IMPLEMENTATION | CODE_TEST, CODE_REVIEW |
| CODE_TEST | CODE_REVIEW, CODE_TEST_FIX, BLOCKED |
| CODE_TEST_FIX | CODE_TEST, BLOCKED |
| CODE_REVIEW | CODE_APPROVED, CODE_REVISE, BLOCKED |
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

| Mode | Members | min_reviews | grace_period_sec |
|------|---------|-------------|------------------|
| full | `settings.py` の `REVIEW_MODES` で定義 | 4 | 0 |
| standard | `settings.py` の `REVIEW_MODES` で定義 | 3 | 0 |
| lite | `settings.py` の `REVIEW_MODES` で定義 | 2 | 0 |
| skip | (none) | 0 | 0 |

### Reviewer Tiers

Reviewer tiers are defined in `settings.py` (`REVIEWER_TIERS`). See `settings.example.py` for defaults.

| Tier | Members |
|------|---------|
| Regular | [] |
| Free | [] |
| Short-context | [] |

## 4. Watchdog Cycle

```mermaid
graph TD
    START["Watchdog Loop<br/>(60s interval)"] --> SCAN["Scan all pipeline.json files"]
    SCAN --> CHECK{"Active issue<br/>found?"}
    CHECK -->|No| IDLE_CHECK{"All projects<br/>DONE/IDLE?"}
    IDLE_CHECK -->|Yes| STOP["Auto-stop<br/>watchdog loop"]
    IDLE_CHECK -->|No| WAIT["Sleep 60s"]
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
    participant M as Human (M)
    participant DB as gokrax CLI
    participant WD as Watchdog
    participant CC as Claude Code
    participant RV as Reviewers
    participant GL as GitLab
    participant DC as Discord

    M->>DB: gokrax triage --project X --issue 42
    DB->>DB: Set state -> TRIAGE
    DB->>DB: Set state -> IDLE
    M->>DB: gokrax run
    DB->>DB: Set state -> INITIALIZE
    DB->>DB: Auto -> DESIGN_PLAN
    DB->>DC: Plan started
    WD->>CC: /new (design plan task)
    CC->>DB: gokrax submit (plan)
    DB->>DB: Set state -> DESIGN_REVIEW
    DB->>DC: Review requested

    par Review Ensemble
        WD->>RV: /new (review task) x N reviewers
        RV->>DB: gokrax review --verdict APPROVE
    end

    DB->>DB: Set state -> DESIGN_APPROVED
    DB->>DB: Auto -> IMPLEMENTATION
    DB->>DC: Implementation started
    WD->>CC: /new (implement task)
    CC->>GL: git push (branch)
    CC->>DB: gokrax submit (code)
    DB->>DB: Set state -> CODE_TEST
    DB->>DC: Tests running
    WD->>CC: /new (test task)
    CC->>DB: tests pass
    DB->>DB: Set state -> CODE_REVIEW
    DB->>DC: Code review requested

    par Review Ensemble
        WD->>RV: /new (review task) x N reviewers
        RV->>DB: gokrax review --verdict APPROVE
    end

    DB->>DB: Set state -> CODE_APPROVED
    DB->>DB: Auto -> MERGE_SUMMARY_SENT
    DB->>DC: Merge summary
    M->>DB: "OK" (approve merge)
    DB->>GL: glab mr merge
    DB->>DB: Set state -> DONE
    DB->>DC: Issue complete
```
