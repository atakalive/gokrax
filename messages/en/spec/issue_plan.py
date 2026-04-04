"""Prompts and notifications for the ISSUE_PLAN state.

Variables:
    project: str              - Project name
    spec_path: str            - Spec file path
    current_rev: str          - Current revision number
    suggestions_text: str     - Reviewer suggestions YAML text
    gitlab: str               - GitLab repository (e.g. "namespace/project")
    spec_filename: str        - Spec file name
    GOKRAX_CLI: str           - gokrax CLI path
"""



def plan(
    project: str, spec_path: str, current_rev: str,
    suggestions_text: str, gitlab: str, spec_filename: str, GOKRAX_CLI: str,
    **_kw,
) -> str:
    """Issue creation prompt (§8.1)."""
    return f"""[INSTRUCTION] Complete this task in one go without interruption. Do not ask for confirmation mid-task.

Merge the following reviewer suggestions and create GitLab Issues.

Project: {project}
Spec: {spec_path} (rev{current_rev})

## Reviewer Suggestions
{suggestions_text}
## Integration Instructions
Merge suggestions from multiple reviewers, eliminate duplicates, and determine the final list of Issues.
Consolidate similar or duplicate Issues into one and organize dependencies.

## Issue Creation Rules
- Prefix Issue titles with `[spec:{spec_filename}:S-{{N}}]` (N is a sequential number).
- Run `glab issue list -R {gitlab} -O json` to check existing Issues and avoid duplicates.
- Do not use Issue comments.
- Each Issue body must include "Expected Behavior" and "Tests" sections.
- Creation command: `glab issue create -R {gitlab} --title "..." --description "..." --label "spec-mode"`
- Note implementation caveats in the body with ⚠️ annotations.
- **[Important] State the spec file path at the top of each Issue. (e.g. `Spec: {spec_path}`)**

## Completion Report Format
```yaml
status: done
created_issues:
  - 51
  - 52
  - 53
```

created_issues is a list of created Issue numbers (integers).

## Submission Method
Save the completion report to a YAML file and submit with the following command:
```
{GOKRAX_CLI} spec issue-submit --pj {project} --file <YAML file path>
```

[IMPORTANT] Complete issue creation and submission without interruption."""


# ---------------------------------------------------------------------------
# Discord notifications (short)
# ---------------------------------------------------------------------------

def notify_done(project: str, issue_count: int, **_kw) -> str:
    """ISSUE_PLAN completed."""
    return f"[Spec][{project}] {issue_count} issues created"
