"""Prompts, notifications, and reminders for the SPEC_REVISE state.

Variables (common):
    project: str           - Project name
    spec_path: str         - Spec file path
    current_rev: str       - Current revision number
    GOKRAX_CLI: str        - gokrax CLI path
"""



# ---------------------------------------------------------------------------
# Agent-facing prompts
# ---------------------------------------------------------------------------

def revise(
    project: str, spec_path: str, current_rev: str, GOKRAX_CLI: str,
    next_rev: int, new_spec_path: str, merged_report_md: str,
    **_kw,
) -> str:
    """Revision prompt (§6.1)."""
    return f"""[INSTRUCTION] Complete this task in one go without interruption. Do not ask for confirmation mid-task.

Revise the following spec.

Project: {project}
Spec (current): {spec_path} (rev{current_rev})
Revision target file: {new_spec_path} (rev{next_rev})

## Revision Steps
1. Copy the current spec `{spec_path}` to create `{new_spec_path}`
2. Edit `{new_spec_path}` (apply revision changes)
3. git add + commit `{new_spec_path}`
4. Create a YAML file following the "Completion Report Format" below and submit with:
   `{GOKRAX_CLI} spec revise-submit --pj {project} --file <YAML file path>`

## Merged Review Report
{merged_report_md}

## Revision Rules
- Add one row to the changelog table
- List all items in the format `[v{next_rev}] finding ID: description`
- In pseudocode, note change reasons with `# [v{next_rev}] Pascal C-1: description`
- Clearly state reasons for deferred findings

## Completion Report Format
```yaml
status: done
new_rev: "{next_rev}"
commit: "<git commit hash, 7+ characters>"
changes:
  added_lines: <number>
  removed_lines: <number>
  reflected_items: ["pascal:C-1", ...]
  deferred_items: ["dijkstra:m-4", ...]
  deferred_reasons:
    "dijkstra:m-4": "Reason"
```

[IMPORTANT] Complete the revision, commit, and submission without interruption."""


def self_review(
    project: str, spec_path: str, current_rev: str, GOKRAX_CLI: str,
    last_commit: str, checklist_text: str, example_yaml: str,
    **_kw,
) -> str:
    """Self-review prompt."""
    return f"""[INSTRUCTION] Complete this task in one go without interruption. Do not ask for confirmation mid-task.

Perform a self-review of the revised spec.

Project: {project}
Spec: {spec_path} (rev{current_rev})
Last commit: {last_commit}

## Checklist
{checklist_text}

## Response Format
Respond with the following YAML. Each item's result must be "Yes" or "No" only.

```yaml
{example_yaml}
```

If result is "No", describe the specific problem in evidence.

## Submission Method
Save the check results to a YAML file and submit with the following command:
{GOKRAX_CLI} spec self-review-submit --pj {project} --file <YAML file path>

Wrapping in a YAML block (```yaml ... ```) is recommended. The CLI will auto-complete fences if omitted, but wrap for reliable parsing.

[IMPORTANT] Complete the check without interruption."""


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

def nudge(project: str, current_rev: str, GOKRAX_CLI: str, **_kw) -> str:
    """Spec revision reminder."""
    return (
        f"[Remind] {project} spec rev{current_rev} revision is incomplete.\n"
        f"Reflect review findings and submit the completion report with the following command:\n"
        f"{GOKRAX_CLI} spec revise-submit --pj {project} --file <completion report YAML file path>"
    )


# ---------------------------------------------------------------------------
# Discord notifications (short)
# ---------------------------------------------------------------------------

def notify_done(project: str, rev: str | int, commit: str, **_kw) -> str:
    """REVISE completed (with commit hash)."""
    return f"[Spec][{project}] rev{rev} revision completed ({commit[:7]})"


def notify_commit_failed(project: str, rev: str | int, **_kw) -> str:
    """REVISE completed (git commit failed)."""
    return f"[Spec][{project}] ⚠️ rev{rev} git commit failed"


def notify_no_changes(project: str, rev: str | int, **_kw) -> str:
    """REVISE completed (zero diff) — SPEC_PAUSED."""
    return f"[Spec][{project}] ⚠️ rev{rev} no changes (empty revision)"


def notify_self_review_failed(project: str, failed_count: int, **_kw) -> str:
    """Self-review sent back notification."""
    return f"[Spec][{project}] 🔁 self-review: {failed_count} issues found. Sent back to implementer"
