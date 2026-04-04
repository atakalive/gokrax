"""Prompts and notifications for the QUEUE_PLAN state.

Variables:
    project: str          - Project name
    spec_path: str        - Spec file path
    issues_text: str      - Created Issue numbers (space-separated)
    queue_file_path: str  - Queue file path
    GOKRAX_CLI: str       - gokrax CLI path
"""



def plan(
    project: str, spec_path: str, issues_text: str, queue_file_path: str, GOKRAX_CLI: str,
    **_kw,
) -> str:
    """Queue generation prompt (§9)."""
    return f"""[INSTRUCTION] Complete this task in one go without interruption. Do not ask for confirmation mid-task.

Register created Issues into the batch execution queue.

Project: {project}
Spec: {spec_path}
Created Issue numbers: {issues_text}
Queue file: {queue_file_path}

## Batch Line Format
```
{{project}} {{issue_nums}} full [--keep-context] # Reason
```

- `issue_nums` are comma-separated (e.g. `{project} 51,52,53 full # Phase 1`)
- Issues within a batch are implemented in parallel. Place Issues with dependencies in separate batches
- review_mode is full / lite. Use lite for straightforward, low-risk changes.

### Select the CC model for implementation based on task difficulty.
- Default: Sonnet (no specification needed)
- If planning is hard but implementation is fine with Sonnet, specify `plan=opus` only.
- If Opus is better for both planning and implementation: `plan=opus` and `impl=opus`

### Context carry-over can be specified as needed. Implementation progresses through DESIGN_REVIEW->IMPLEMENTATION->CODE_REVIEW.
- `--keep-ctx-intra` carries context from DESIGN review to CODE review
- `--keep-ctx-batch` carries context from the previous batch's CODE review to the next batch's DESIGN review
- `--keep-ctx-all` carries both batch and intra context (i.e. no context reset)

- Place Issues with dependencies in separate batches
- Group simple, parallelizable Issues on the same line
- Do not over-optimize costs for difficult tasks. Balance cost and quality.

## Registration Steps
1. Append batch lines to the end of the existing queue file ({queue_file_path})
2. Analyze Issue dependencies and determine appropriate batch splits

## Completion Report Format
```yaml
status: done
batches: 3
queue_file: "{queue_file_path}"
```

batches is the number of appended batch lines (integer >= 1).

## Submission Method
Save the completion report to a YAML file and submit with the following command:
```
{GOKRAX_CLI} spec queue-submit --pj {project} --file <YAML file path>
```

[IMPORTANT] Complete queue registration and submission without interruption."""


# ---------------------------------------------------------------------------
# Discord notifications
# ---------------------------------------------------------------------------

def notify_done(project: str, batch_count: int, **_kw) -> str:
    """QUEUE_PLAN completed."""
    return f"[Spec][{project}] {batch_count} batches queued"
