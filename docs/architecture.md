# gokrax — Architecture & State Machine Diagrams

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
        CC1["Claude Code CLI<br/>(Impl Lead 1: Kaneko)"]
        CC2["Claude Code CLI<br/>(Impl Lead 2: Neumann)"]
    end

    subgraph Review["Reviewer Ensemble"]
        direction TB
        R_REG["Regular Tier<br/>Leibniz · Dijkstra · Euler · Basho"]
        R_SEMI["Semi Tier<br/>Pascal"]
        R_FREE["Free Tier<br/>Hanfei"]
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
    WD -->|"request review"| R_SEMI
    WD -->|"request review<br/>(ping → fallback)"| R_FREE
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
        FULL["<b>full</b> (5 reviewers)<br/>Pascal · Leibniz · Dijkstra · Euler · Basho"]
        STD["<b>standard</b> (4 reviewers)<br/>Pascal · Leibniz · Dijkstra · Basho"]
        LITE3["<b>lite3</b> (3 reviewers)<br/>Leibniz · Pascal · Euler"]
        LITE["<b>lite</b> (2 reviewers)<br/>Euler · Pascal"]
        MIN["<b>min</b> (1 reviewer)<br/>Leibniz"]
        SKIP["<b>skip</b> (0 reviewers)<br/>auto-approve"]
    end

    subgraph "Reviewer Tiers"
        REG["🟢 <b>Regular</b><br/>Leibniz (GPT-5.4)<br/>Dijkstra (Opus)<br/>Euler (GPT-5.4)<br/>Basho (Local Qwen3.5-27B)"]
        SEMI["🟡 <b>Semi</b><br/>Pascal (Gemini 3 Pro)"]
        FREE["🔴 <b>Free</b><br/>Hanfei (Qwen Portal)"]
    end

    subgraph "Dispatch Logic"
        D1["1. Send /new to all members"]
        D2["2. Regular: wait for response"]
        D3["3. Semi: wait, no ping"]
        D4["4. Free: ping after 20s<br/>   → no response → exclude"]
        D5["5. Collect until min_reviews met"]
        D6["6. Aggregate verdicts<br/>   (worst severity wins)"]
    end

    D1 --> D2 --> D3 --> D4 --> D5 --> D6
```

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
    DB->>DB: Set state → DESIGN_PLAN
    DB->>DC: 📋 Plan started
    WD->>CC: /new (design plan task)
    CC->>DB: gokrax submit (plan)
    DB->>DB: Set state → DESIGN_REVIEW
    DB->>DC: 📝 Review requested

    par Review Ensemble
        WD->>RV: /new (review task) × N reviewers
        RV->>DB: gokrax review --verdict APPROVE
    end

    DB->>DB: Set state → DESIGN_APPROVED
    DB->>DB: Auto → IMPLEMENTATION
    DB->>DC: 🔨 Implementation started
    WD->>CC: /new (implement task)
    CC->>GL: git push (branch)
    CC->>DB: gokrax submit (code)
    DB->>DB: Set state → CODE_REVIEW
    DB->>DC: 🔍 Code review requested

    par Review Ensemble
        WD->>RV: /new (review task) × N reviewers
        RV->>DB: gokrax review --verdict APPROVE
    end

    DB->>DB: Set state → CODE_APPROVED
    DB->>DB: Auto → MERGE_SUMMARY_SENT
    DB->>DC: 📊 Merge summary
    M->>DB: "OK" (approve merge)
    DB->>GL: glab mr merge
    DB->>DB: Set state → DONE
    DB->>DC: ✅ Issue complete
```
