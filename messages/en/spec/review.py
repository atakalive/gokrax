"""Prompts, notifications, and reminders for the SPEC_REVIEW state.

Variables (common):
    project: str       - Project name
    spec_path: str     - Spec file path
    current_rev: str   - Current revision number
    GOKRAX_CLI: str    - gokrax CLI path
    reviewer: str      - Reviewer name (for file path generation)
"""

import re


# ---------------------------------------------------------------------------
# Agent-facing prompts
# ---------------------------------------------------------------------------

def initial(project: str, spec_path: str, current_rev: str, GOKRAX_CLI: str, reviewer: str = "", **_kw) -> str:
    """Initial review prompt (§5.1)."""
    sanitized = re.sub(r'[/\\\s]', '-', project)
    save_path = f"/tmp/gokrax-review/{sanitized}--spec-{reviewer}-rev{current_rev}.yaml" if reviewer else f"/tmp/gokrax-review/{sanitized}--spec-<YOUR_NAME>-rev{current_rev}.yaml"
    return f"""[INSTRUCTION] Complete this task in one go without interruption. Do not ask for confirmation mid-task.

Review the following spec. This is an **exhaustive review** request.

Project: {project}
Spec: {spec_path} (rev{current_rev})

## Review Instructions
- Assign severity to every finding: 🔴 Critical (P0) / 🟠 Major (P1) / 🟡 Minor / 💡 Suggestion
- Specify section numbers (e.g. §6.2)
- Pay special attention to consistency between pseudocode sections
- Also verify consistency with the existing gokrax codebase
- Look for state machine transition gaps and deadlocks
- Include only **one** YAML block in your response
- Verdict selection: critical → P0, major → P1, minor/suggestion → P2. Use APPROVE only when you have zero findings

## Output Format
```yaml
verdict: APPROVE | P0 | P1 | P2
items:
  - id: C-1
    severity: critical | major | minor | suggestion
    section: "§6.2"
    title: "Title"
    description: "Description"
    suggestion: "Suggested fix"
```

## Submission Instructions
1. Save the YAML file to: {save_path}
2. Submit with the following command:
```bash
{GOKRAX_CLI} spec review-submit --pj {project} --reviewer {reviewer or "<YOUR_NAME>"} --file {save_path}
```

The file can be raw YAML or Markdown containing a ```yaml ... ``` block matching the output format above.

[IMPORTANT] Complete the review and submit results without interruption."""


def revision(
    project: str, spec_path: str, current_rev: str, GOKRAX_CLI: str, reviewer: str = "",
    changelog: str = "", added: str = "", removed: str = "", last_commit: str = "",
    **_kw,
) -> str:
    """Review prompt for rev2+ (§5.1)."""
    sanitized = re.sub(r'[/\\\s]', '-', project)
    save_path = f"/tmp/gokrax-review/{sanitized}--spec-{reviewer}-rev{current_rev}.yaml" if reviewer else f"/tmp/gokrax-review/{sanitized}--spec-<YOUR_NAME>-rev{current_rev}.yaml"
    return f"""[INSTRUCTION] Complete this task in one go without interruption. Do not ask for confirmation mid-task.

Review the revised version of the following spec.

Project: {project}
Spec: {spec_path} (rev{current_rev})
Changes since last review: +{added} lines, -{removed} lines
Last commit: {last_commit}

## Changes Since Last Review
{changelog}

## Review Instructions
- Verify that previous findings have been properly addressed
- Check new additions for issues
- Severity, section numbers, and YAML format are the same as before
- Include only **one** YAML block in your response
- Verdict selection: critical → P0, major → P1, minor/suggestion → P2. Use APPROVE only when you have zero findings

## Submission Instructions
1. Save the YAML file to: {save_path}
2. Submit with the following command:
```bash
{GOKRAX_CLI} spec review-submit --pj {project} --reviewer {reviewer or "<YOUR_NAME>"} --file {save_path}
```

The file can be raw YAML or Markdown containing a ```yaml ... ``` block matching the output format above.

[IMPORTANT] Complete the review and submit results without interruption."""


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

def nudge(project: str, current_rev: str, spec_path: str, reviewer: str, GOKRAX_CLI: str, **_kw) -> str:
    """Spec review reminder."""
    sanitized = re.sub(r'[/\\\s]', '-', project)
    save_path = f"/tmp/gokrax-review/{sanitized}--spec-{reviewer}-rev{current_rev}.yaml"
    return (
        f"[Remind] {project} spec rev{current_rev} review is incomplete.\n"
        f"Spec: {spec_path}\n"
        f"Submit review results with the following command:\n"
        f"{GOKRAX_CLI} spec review-submit --pj {project} --reviewer {reviewer} --file {save_path}"
    )


# ---------------------------------------------------------------------------
# Discord notifications (short)
# ---------------------------------------------------------------------------

def notify_start(project: str, rev: str | int, reviewer_count: int, **_kw) -> str:
    """SPEC_REVIEW started."""
    return f"[Spec][{project}] rev{rev} review started ({reviewer_count} reviewers)"


def notify_complete(
    project: str, rev: str | int,
    critical: int, major: int, minor: int, suggestion: int,
    **_kw,
) -> str:
    """Transition to SPEC_REVISE."""
    return f"[Spec][{project}] rev{rev} review completed — C:{critical} M:{major} m:{minor} s:{suggestion}"


def notify_failed(project: str, rev: str | int, **_kw) -> str:
    """Transition to SPEC_REVIEW_FAILED."""
    return f"[Spec][{project}] ❌ insufficient valid reviews"
