# DevBar — Architecture & State Machine Diagrams

## 1. System Architecture (Overall Flow)

```mermaid
graph LR
    subgraph Input
        GL[("GitLab\nIssues")]
        M["Human\nOperator"]
    end

    subgraph DevBar["DevBar (Pipeline Orchestrator)"]
        CLI["devbar CLI"]
        SM["State Machine\n(pipeline.json)"]
        WD["Watchdog\n(polling loop)"]
        TQ["Task Queue\n(batch execution)"]
    end

    subgraph Execution
        CC1["Claude Code CLI\n(Impl Lead 1: Kaneko)"]
        CC2["Claude Code CLI\n(Impl Lead 2: Neumann)"]
    end

    subgraph Review["Reviewer Ensemble"]
        direction TB
        R_REG["Regular Tier\nLeibniz · Dijkstra · Euler · Basho"]
        R_SEMI["Semi Tier\nPascal"]
        R_FREE["Free Tier\nHanfei"]
    end

    subgraph Output
        MR[("GitLab\nMerge Request")]
        DC["Discord\nNotifications"]
        WB["WatcherB\n(GUI Monitor)"]
    end

    GL -->|"issue created"| CLI
    M -->|"devbar triage/run"| CLI
    CLI --> SM
    SM <--> WD
    WD -->|"dispatch task"| TQ
    TQ -->|"design/implement"| CC1
    TQ -->|"design/implement"| CC2
    WD -->|"request review"| R_REG
    WD -->|"request review"| R_SEMI
    WD -->|"request review\n(ping → fallback)"| R_FREE
    R_REG -->|"verdict"| CLI
    R_SEMI -->|"verdict"| CLI
    R_FREE -->|"verdict"| CLI
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

    IDLE --> DESIGN_PLAN : triage / run

    state "Design Phase" as design {
        DESIGN_PLAN --> DESIGN_REVIEW : plan submitted
        DESIGN_REVIEW --> DESIGN_APPROVED : all reviewers APPROVE
        DESIGN_REVIEW --> DESIGN_REVISE : P0/P1/REJECT verdict
        DESIGN_REVISE --> DESIGN_REVIEW : revision submitted
        DESIGN_REVIEW --> BLOCKED : timeout / stall
    }

    state "Implementation Phase" as impl {
        IMPLEMENTATION --> CODE_REVIEW : code submitted
        CODE_REVIEW --> CODE_APPROVED : all reviewers APPROVE
        CODE_REVIEW --> CODE_REVISE : P0/P1/REJECT verdict
        CODE_REVISE --> CODE_REVIEW : revision submitted
        CODE_REVIEW --> BLOCKED : timeout / stall
    }

    DESIGN_APPROVED --> IMPLEMENTATION : auto-transition

    CODE_APPROVED --> MERGE_SUMMARY_SENT : auto-transition
    MERGE_SUMMARY_SENT --> DONE : human approves merge

    DONE --> IDLE : next issue
    BLOCKED --> IDLE : manual reset

    note right of DESIGN_REVISE : MAX_REVISE_CYCLES = 3
    note right of CODE_REVISE : MAX_REVISE_CYCLES = 3
```

## 3. Review Ensemble Detail

```mermaid
graph TB
    subgraph "Review Modes (per-project configurable)"
        FULL["<b>full</b> (5 reviewers)\nPascal · Leibniz · Dijkstra · Euler · Basho"]
        STD["<b>standard</b> (4 reviewers)\nPascal · Leibniz · Dijkstra · Basho"]
        LITE3["<b>lite3</b> (3 reviewers)\nLeibniz · Pascal · Euler"]
        LITE["<b>lite</b> (2 reviewers)\nEuler · Pascal"]
        MIN["<b>min</b> (1 reviewer)\nLeibniz"]
        SKIP["<b>skip</b> (0 reviewers)\nauto-approve"]
    end

    subgraph "Reviewer Tiers"
        REG["🟢 <b>Regular</b>\nLeibniz (GPT-5.4)\nDijkstra (Opus)\nEuler (GPT-5.4)\nBasho (Local Qwen3.5-27B)"]
        SEMI["🟡 <b>Semi</b>\nPascal (Gemini 3 Pro)"]
        FREE["🔴 <b>Free</b>\nHanfei (Qwen Portal)"]
    end

    subgraph "Dispatch Logic"
        D1["1. Send /new to all members"]
        D2["2. Regular: wait for response"]
        D3["3. Semi: wait, no ping"]
        D4["4. Free: ping after 20s\n   → no response → exclude"]
        D5["5. Collect until min_reviews met"]
        D6["6. Aggregate verdicts\n   (worst severity wins)"]
    end

    D1 --> D2 --> D3 --> D4 --> D5 --> D6
```

## 4. Watchdog Cycle

```mermaid
graph TD
    START["Watchdog Loop\n(60s interval)"] --> SCAN["Scan all pipeline.json files"]
    SCAN --> CHECK{"Active issue\nfound?"}
    CHECK -->|No| IDLE_CHECK{"All projects\nDONE/IDLE?"}
    IDLE_CHECK -->|Yes| STOP["Auto-stop\nwatchdog loop"]
    IDLE_CHECK -->|No| WAIT["Sleep 60s"]
    CHECK -->|Yes| TIMEOUT{"Timed out?"}
    TIMEOUT -->|Yes| NUDGE["Send nudge /\nauto-transition\nto BLOCKED"]
    TIMEOUT -->|No| PENDING{"Pending\nnotification?"}
    PENDING -->|Yes| NOTIFY["Send Discord\nnotification"]
    PENDING -->|No| WAIT
    NUDGE --> WAIT
    NOTIFY --> WAIT
    WAIT --> SCAN
```

## 5. End-to-End Issue Lifecycle (Sequence)

```mermaid
sequenceDiagram
    participant M as Human (M)
    participant DB as DevBar CLI
    participant WD as Watchdog
    participant CC as Claude Code
    participant RV as Reviewers
    participant GL as GitLab
    participant DC as Discord

    M->>DB: devbar triage --project X --issue 42
    DB->>DB: Set state → DESIGN_PLAN
    DB->>DC: 📋 Plan started
    WD->>CC: /new (design plan task)
    CC->>DB: devbar submit (plan)
    DB->>DB: Set state → DESIGN_REVIEW
    DB->>DC: 📝 Review requested

    par Review Ensemble
        WD->>RV: /new (review task) × N reviewers
        RV->>DB: devbar review --verdict APPROVE
    end

    DB->>DB: Set state → DESIGN_APPROVED
    DB->>DB: Auto → IMPLEMENTATION
    DB->>DC: 🔨 Implementation started
    WD->>CC: /new (implement task)
    CC->>GL: git push (branch)
    CC->>DB: devbar submit (code)
    DB->>DB: Set state → CODE_REVIEW
    DB->>DC: 🔍 Code review requested

    par Review Ensemble
        WD->>RV: /new (review task) × N reviewers
        RV->>DB: devbar review --verdict APPROVE
    end

    DB->>DB: Set state → CODE_APPROVED
    DB->>DB: Auto → MERGE_SUMMARY_SENT
    DB->>DC: 📊 Merge summary
    M->>DB: "OK" (approve merge)
    DB->>GL: glab mr merge
    DB->>DB: Set state → DONE
    DB->>DC: ✅ Issue complete
```
