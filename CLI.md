# gokrax CLI Manual

> `gokrax <command> [options]`
> `--pj` is an alias for `--project` in all commands. Noted once here and not repeated in every table below.

---

## Basic Flow: Batch Start to Completion

```bash
# 1. Start batch (triage + transition to DESIGN_PLAN + enable watchdog)
gokrax start --pj myproject --issue 17 18 19 --mode full

# 2. [Implementer] Review/edit issue descriptions, then report design plan done
gokrax plan-done --pj myproject --issue 17 18 19

# --- Automated from here ---
# watchdog: DESIGN_PLAN -> DESIGN_REVIEW (reviewers notified)
# watchdog: all reviews done -> DESIGN_APPROVED -> IMPLEMENTATION (CC auto-starts)
# CC: Plan -> Impl -> gokrax commit (automatic)
# watchdog: IMPLEMENTATION -> CODE_REVIEW (reviewers notified)
# watchdog: all reviews done -> CODE_APPROVED -> MERGE_SUMMARY_SENT

# 3. [Owner] Reply "OK" to the summary in the dev channel -> DONE -> git push + issue close -> IDLE
```

---

## Command List

### `status` -- Show all project status

```bash
gokrax status
```

Output example:
```
[ON] myproject: CODE_REVIEW  issues=[#17, #18]  ReviewerSize=full  Reviewers=["reviewer-a", "reviewer-b", "reviewer-c", "reviewer-d"]
  #17: 2/3 reviews (1 APPROVE, 1 P0)
  #18: 3/3 reviews (3 APPROVE)
[OFF] another-project: IDLE  issues=[none]  ReviewerSize=lite  Reviewers=["reviewer-a", "reviewer-b"]
```

No options (other than `-h`).

### `init` -- Initialize a new project

```bash
gokrax init --pj myproject --gitlab user/myproject --repo-path /path/to/repo
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pj` | Yes | project name |
| `--gitlab GITLAB` | No | GitLab path (default: `<user>/<project>`) |
| `--repo-path REPO_PATH` | No | local repository path |
| `--implementer IMPLEMENTER` | No | implementer agent ID |

### `enable` / `disable` -- Watchdog control

```bash
gokrax enable --pj myproject
gokrax disable --pj myproject
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pj` | Yes | project name |

### `extend` -- Extend timeout

```bash
gokrax extend --pj myproject --by 600
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pj` | Yes | project name |
| `--by BY` | No | seconds to add (default: 600) |

Applicable states: DESIGN_PLAN, DESIGN_REVISE, IMPLEMENTATION, CODE_REVISE. Max 2 extensions.

### `start` -- Start a batch

```bash
# Specify issue numbers
gokrax start --pj myproject --issue 17 18 19 --mode full

# Auto-fetch all open issues from GitLab
gokrax start --pj myproject --mode standard
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pj` | Yes | project name |
| `--issue N [N ...]` | No | issue numbers (omit to fetch all open issues from GitLab) |
| `--mode {full,standard,lite,min,skip}` | No | review mode (omit to keep current setting) |
| `--keep-ctx-batch` | No | keep context within the batch |
| `--keep-ctx-intra` | No | keep context within intra-issue steps |
| `--keep-ctx-all` | No | keep context across all steps |
| `--keep-ctx-none` | No | discard context between steps |
| `--p2-fix` | No | enable P2-fix mode (auto-revise on P2 verdicts) |
| `--comment=COMMENT` | No | Batch-wide note (e.g., domain-specific risk) injected into prompts. Must be the last argument. |
| `--skip-cc-plan` | No | skip CC plan phase, go directly to implementation |
| `--no-skip-cc-plan` | No | explicitly do not skip CC plan phase |
| `--skip-test` | No | skip CODE_TEST phase, go directly to CODE_REVIEW |
| `--no-skip-test` | No | explicitly do not skip CODE_TEST phase |
| `--skip-assess` | No | skip ASSESSMENT phase, go directly to IMPLEMENTATION |
| `--no-skip-assess` | No | explicitly do not skip ASSESSMENT phase |

Prerequisite: project must be in IDLE state.

### `transition` -- Manual state transition

```bash
# Normal transition (with validation)
gokrax transition --pj myproject --to CODE_REVIEW

# Force transition (skip validation)
gokrax transition --pj myproject --to IDLE --force

# Resume (skip validation + prefix notifications with "(resumed)")
gokrax transition --pj myproject --to DESIGN_PLAN --resume
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pj` | Yes | project name |
| `--to TO` | Yes | target state |
| `--actor ACTOR` | No | transition actor (default: `cli`) |
| `--force` | No | skip transition validation |
| `--resume` | No | skip validation and prefix notifications with (resumed) |
| `--dry-run` | No | apply transition only, skip notifications (for testing) |

### `reset` -- Reset all projects to IDLE

```bash
gokrax reset
gokrax reset --dry-run
gokrax reset --force
```

| Option | Required | Description |
|--------|----------|-------------|
| `--dry-run` | No | show targets without making changes |
| `--force` | No | skip confirmation prompt |

### `review` -- Record review result

```bash
gokrax review \
  --pj myproject \
  --issue 17 \
  --round 1 \
  --reviewer alice \
  --verdict APPROVE \
  --summary 'Design is sound. Boundary conditions handled properly.'
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pj` | Yes | project name |
| `--issue N` | Yes | issue number |
| `--reviewer` | Yes | reviewer name: (configured reviewers) |
| `--verdict` | Yes | verdict: `{APPROVE,P0,P1,P2,REJECT}` |
| `--summary SUMMARY` | No | review summary |
| `--force` | No | overwrite existing review |
| `--round ROUND` | No | review round number (auto-filled) |

Idempotent: duplicate submissions from the same reviewer are skipped.
GitLab integration: verdict + summary are posted as an issue note.

### `flag` -- Human verdict injection

```bash
gokrax flag --pj myproject --issue 17 --verdict P0 --summary "Critical bug found"
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pj` | Yes | project name |
| `--issue N` | Yes | issue number |
| `--verdict` | Yes | verdict: `{P0,P1,P2}` |
| `--summary SUMMARY` | No | flag description |

Can be used at any time regardless of current state.

### `dispute` -- Dispute a verdict

```bash
gokrax dispute --pj myproject --issue 17 --reviewer alice --reason "False positive"
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pj` | Yes | project name |
| `--issue N` | Yes | issue number |
| `--reviewer` | Yes | reviewer name: (configured reviewers) |
| `--reason REASON` | Yes | reason for the dispute |

Used during REVISE to dispute a P0/P1 verdict.

### `commit` -- Record implementation commit

```bash
gokrax commit --pj myproject --issue 17 18 19 --hash abc1234
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pj` | Yes | project name |
| `--issue N [N ...]` | Yes | issue numbers (multiple allowed) |
| `--hash HASH` | Yes | git commit hash |
| `--session-id SESSION_ID` | No | CC session ID |

List all issue numbers when a single commit resolves multiple issues.

### `plan-done` -- Mark design plan as done

```bash
gokrax plan-done --pj myproject --issue 17 18 19
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pj` | Yes | project name |
| `--issue N [N ...]` | Yes | issue numbers (multiple allowed) |

Only valid in DESIGN_PLAN state. Run after the implementer has reviewed and edited issue descriptions.

### `assess-done` -- Record assessment result

```bash
gokrax assess-done --pj myproject --level 3 --summary "複数モジュールにまたがる変更"
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pj` | Yes | project name |
| `--level N` | Yes | difficulty level (1-5) |
| `--summary TEXT` | No | assessment summary (max 500 chars) |

Prerequisite: project must be in ASSESSMENT state.
Records a batch-level assessment in pipeline JSON and prepends `[Lvl N]` to each issue title.
Title update failure is a warning only — transition to IMPLEMENTATION proceeds regardless.
Triggers transition to IMPLEMENTATION on next watchdog cycle.

### `design-revise` -- Mark design revision as done

```bash
gokrax design-revise --pj myproject --issue 17
gokrax design-revise --pj myproject --issue 17 18
gokrax design-revise --pj myproject --issue 17 --comment "Fixed P0 issue in design"
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pj` | Yes | project name |
| `--issue N [N ...]` | Yes | issue numbers (multiple allowed) |
| `--comment COMMENT` | No | comment to post as GitLab issue note (optional) |

Only valid in DESIGN_REVISE state.

### `code-revise` -- Mark code revision as done

```bash
gokrax code-revise --pj myproject --issue 17 --hash f8f7c30
gokrax code-revise --pj myproject --issue 17 18 19 --hash f8f7c30
gokrax code-revise --pj myproject --issue 17 --hash f8f7c30 --comment "Added zero-division guard"
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pj` | Yes | project name |
| `--issue N [N ...]` | Yes | issue numbers (multiple allowed) |
| `--hash HASH` | Yes | git commit hash |
| `--comment COMMENT` | No | comment to post as GitLab issue note (optional) |

Only valid in CODE_REVISE state. Records commit hash and sets code_revised flag in one step.

### `review-mode` -- Change review mode

```bash
gokrax review-mode --pj myproject --mode full
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pj` | Yes | project name |
| `--mode` | Yes | review mode: `{full,standard,lite,skip}` |

Reviewer membership for each mode is configured in `config`, not hardcoded in CLI.

### `merge-summary` -- Post merge summary

```bash
gokrax merge-summary --pj myproject
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pj` | Yes | project name |

Only valid in CODE_APPROVED state. Normally posted automatically by watchdog.

### `cc-start` -- Record CC process PID

```bash
gokrax cc-start --pj myproject --pid 12345
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pj` | Yes | project name |
| `--pid PID` | Yes | CC process PID |

Normally recorded automatically by watchdog.

### `qrun` -- Run next batch from queue

```bash
gokrax qrun
gokrax qrun --dry-run
```

| Option | Required | Description |
|--------|----------|-------------|
| `--queue QUEUE` | No | queue file path (default: `gokrax-queue.txt`) |
| `--dry-run` | No | show entry without executing |

### `qstatus` -- Show active queue entries

```bash
gokrax qstatus
```

| Option | Required | Description |
|--------|----------|-------------|
| `--queue QUEUE` | No | queue file path |

### `qadd` -- Add entries to queue

```bash
gokrax qadd myproject 33,34 lite no-automerge comment=note
gokrax qadd --file entries.txt
echo "myproject 33 full" | gokrax qadd --stdin
```

| Option | Required | Description |
|--------|----------|-------------|
| `entry ...` (positional) | No | entry to add (e.g. `myproject 33,34 lite no-automerge comment=note`) |
| `--file FILE` | No | file containing entries (one per line) |
| `--stdin` | No | read entries from stdin |
| `--queue QUEUE` | No | queue file path |

### `qdel` -- Delete a queue entry

```bash
gokrax qdel 0
gokrax qdel last
```

| Option | Required | Description |
|--------|----------|-------------|
| `target` (positional) | Yes | target to delete (index number or `last`) |
| `--queue QUEUE` | No | queue file path |

### `qedit` -- Replace a queue entry

```bash
gokrax qedit 0 myproject 105 full automerge
gokrax qedit last myproject 50 standard
```

| Option | Required | Description |
|--------|----------|-------------|
| `target` (positional) | Yes | target to replace (index number or `last`) |
| `entry ...` (positional) | Yes | new entry (e.g. `myproject 105 full automerge`) |
| `--queue QUEUE` | No | queue file path |

---

## Verdicts

| Verdict | When to use |
|---------|-------------|
| **APPROVE** | No issues found. Approved. |
| **P0** | Critical / blocker. This issue goes back to REVISE. |
| **P1** | Major. Blocks progress. |
| **P2** | Minor / suggestion. In `--p2-fix` mode, triggers REVISE up to MAX_TURN attempts. |
| **REJECT** | Fundamentally flawed. Equivalent to P0 (rarely used). |

---

## Skill Settings

Skills are external prompt files injected into reviewer/implementer messages. Configured in `settings.py`.

### SKILLS

Maps skill names to file paths:

```python
SKILLS: dict[str, str] = {
    "example-skill": str(Path.home() / ".openclaw/skills/example-skill/SKILL.md"),
}
```

### AGENT_SKILLS

Maps agent names to per-phase skill lists. `phase` is `"design"` (design review) or `"code"` (code review / implementation).

```python
AGENT_SKILLS: dict[str, dict[str, list[str]]] = {
    "reviewer1": {
        "design": [],
        "code": ["diff-reading-guide", "numerical-validation"],
    },
}
```

> **Deprecated**: The old `dict[str, list[str]]` format (e.g. `{"reviewer1": ["skill-a"]}`) still works as a fallback — all listed skills are applied to every phase — but emits a deprecation warning. Migrate to the new per-phase format.

### PROJECT_SKILLS

Maps project names to per-phase skill lists. These are merged (union) with `AGENT_SKILLS`:

```python
PROJECT_SKILLS: dict[str, dict[str, list[str]]] = {
    "EMCalibrator": {
        "design": ["device-safety"],
        "code": ["device-safety"],
    },
}
```

When both `AGENT_SKILLS` and `PROJECT_SKILLS` specify the same skill name, it is included only once (deduplicated).

---

## Log Paths

```bash
# watchdog log
tail -f /tmp/gokrax-watchdog.log

# pipeline JSON
cat ~/.openclaw/shared/pipelines/<project>.json | python3 -m json.tool
```

---

## Spec Mode (Specification Review Pipeline)

Automates: spec review -> revision loop -> issue creation -> queue generation.

### Basic Flow

```bash
# 1. Start spec mode pipeline
gokrax spec start --pj myproject \
  --spec docs/SPEC.md --implementer my-agent

# --- Automated from here ---
# watchdog: SPEC_REVIEW (reviewers notified)
# watchdog: P0 found -> SPEC_REVISE (implementer gets revision instructions)
# watchdog: revision done -> SPEC_REVIEW (re-review)
# ... REVIEW <-> REVISE loop (up to max-cycles)
# watchdog: all APPROVE -> SPEC_APPROVED

# 2. [OWNER] After review, proceed to issue creation phase
gokrax spec continue --pj myproject

# --- Automated ---
# ISSUE_SUGGESTION -> ISSUE_PLAN -> QUEUE_PLAN -> SPEC_DONE

# 3. Complete -> return to IDLE
gokrax spec done --pj myproject
```

### Spec Subcommands

#### `spec start` -- Start spec mode pipeline

```bash
gokrax spec start --pj <PROJECT> --spec <PATH> --implementer <AGENT>
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pj` | Yes | project name |
| `--spec SPEC` | Yes | path to spec file (repo-relative) |
| `--implementer IMPLEMENTER` | Yes | revision agent ID |
| `--review-only` | No | review only, do not proceed to issue creation |
| `--no-queue` | No | skip queue generation |
| `--skip-review` | No | skip review (immediately APPROVED) |
| `--max-cycles MAX_CYCLES` | No | max REVIEW <-> REVISE loop count |
| `--review-mode {full,standard,lite,min}` | No | review mode |
| `--model MODEL` | No | CC model override |
| `--auto-continue` | No | skip owner confirmation after APPROVED, auto-proceed to ISSUE_SUGGESTION |
| `--auto-qrun` | No | auto-run queue after spec completion |
| `--rev REV` | No | initial current_rev value (default: 1) |

#### `spec stop` -- Force-stop spec mode

```bash
gokrax spec stop --pj <PROJECT>
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pj` | Yes | project name |

Stops spec mode from any state and returns to IDLE. Disables watchdog.

#### `spec approve` -- Manually approve spec

```bash
gokrax spec approve --pj <PROJECT> [--force]
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pj` | Yes | project name |
| `--force` | No | force approve even if min_reviews not reached |

#### `spec continue` -- Proceed to issue creation

```bash
gokrax spec continue --pj <PROJECT>
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pj` | Yes | project name |

Only valid in SPEC_APPROVED state.

#### `spec done` -- Complete spec mode

```bash
gokrax spec done --pj <PROJECT>
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pj` | Yes | project name |

Transitions from SPEC_DONE to IDLE.

#### `spec retry` -- Retry from FAILED

```bash
gokrax spec retry --pj <PROJECT>
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pj` | Yes | project name |

Transitions from FAILED back to SPEC_REVIEW.

#### `spec resume` -- Resume from PAUSED

```bash
gokrax spec resume --pj <PROJECT>
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pj` | Yes | project name |

Returns to the state before pause.

#### `spec extend` -- Extend stalled spec

```bash
gokrax spec extend --pj <PROJECT> [--cycles 2]
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pj` | Yes | project name |
| `--cycles CYCLES` | No | additional cycles to add (default: 2) |

Transitions from STALLED back to SPEC_REVISE with increased max_cycles.

#### `spec status` -- Show spec mode status

```bash
gokrax spec status --pj <PROJECT>
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pj` | Yes | project name |

#### `spec review-submit` -- Submit review result

```bash
gokrax spec review-submit --pj <PROJECT> --reviewer <REVIEWER> --file <FILE>
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pj` | Yes | project name |
| `--reviewer REVIEWER` | Yes | reviewer name |
| `--file FILE` | Yes | review result YAML file |

Prerequisite: SPEC_REVIEW state. Accepts raw YAML or fenced YAML.

#### `spec revise-submit` -- Submit revision result

```bash
gokrax spec revise-submit --pj <PROJECT> --file <FILE>
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pj` | Yes | project name |
| `--file FILE` | Yes | revision completion YAML file |

Prerequisite: SPEC_REVISE state.

#### `spec self-review-submit` -- Submit self-review result

```bash
gokrax spec self-review-submit --pj <PROJECT> --file <FILE>
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pj` | Yes | project name |
| `--file FILE` | Yes | self-review result file |

#### `spec issue-submit` -- Submit issue plan result

```bash
gokrax spec issue-submit --pj <PROJECT> --file <FILE>
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pj` | Yes | project name |
| `--file FILE` | Yes | ISSUE_PLAN completion YAML file |

Prerequisite: ISSUE_PLAN state.

#### `spec queue-submit` -- Submit queue plan result

```bash
gokrax spec queue-submit --pj <PROJECT> --file <FILE>
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pj` | Yes | project name |
| `--file FILE` | Yes | QUEUE_PLAN completion YAML file |

Prerequisite: QUEUE_PLAN state.

#### `spec suggestion-submit` -- Submit reviewer suggestion

```bash
gokrax spec suggestion-submit --pj <PROJECT> --reviewer <REVIEWER> --file <FILE>
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pj` | Yes | project name |
| `--reviewer REVIEWER` | Yes | reviewer name |
| `--file FILE` | Yes | issue suggestion YAML file |

Prerequisite: ISSUE_SUGGESTION state.

### Spec Mode State Transitions

```
SPEC_REVIEW <-> SPEC_REVISE (up to max-cycles)
    | all APPROVE
SPEC_APPROVED
    | spec continue
ISSUE_SUGGESTION -> ISSUE_PLAN -> QUEUE_PLAN -> SPEC_DONE
    | spec done
IDLE
```

Special states:
- **STALLED**: max-cycles reached. Use `spec extend` to continue.
- **FAILED**: error occurred. Use `spec retry` to return to SPEC_REVIEW.
- **PAUSED**: manually paused. Use `spec resume` to return to previous state.
