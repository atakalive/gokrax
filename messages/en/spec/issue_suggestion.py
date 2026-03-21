"""Prompts for the ISSUE_SUGGESTION state.

Variables:
    project: str       - Project name
    spec_path: str     - Spec file path
    current_rev: str   - Current revision number
    reviewer: str      - Reviewer name
    GOKRAX_CLI: str    - gokrax CLI path
"""



def suggestion(
    project: str, spec_path: str, current_rev: str, reviewer: str, GOKRAX_CLI: str,
    **_kw,
) -> str:
    """Issue breakdown suggestion prompt (§7)."""
    return f"""[INSTRUCTION] Complete this task in one go without interruption. Do not ask for confirmation mid-task.

Based on the approved spec, propose a breakdown into GitLab Issues.

Project: {project}
Spec: {spec_path} (rev{current_rev})

## Request
Break down the spec into implementable Issue units and organize them by phase.
Each Issue must be an independently implementable and reviewable unit.
1 Issue = 1 PR = 1 clear goal. Split large Issues.
If existing code changes are extensive, separate refactoring and feature additions into different Issues.

## Output Format
```yaml
phases:
  - name: "Phase 1: Foundation"
    issues:
      - title: "Implementation title"
        files:
          - "path/to/file.py"
        lines: "100-200"
        spec_refs:
          - "§6.1"
        depends_on: []
      - title: "Another implementation title"
        files:
          - "path/to/other.py"
        lines: ""
        spec_refs:
          - "§7"
        depends_on:
          - "Implementation title"
  - name: "Phase 2: Integration & Testing"
    issues:
      - title: "Integration test implementation"
        files:
          - "tests/test_foo.py"
        lines: ""
        spec_refs:
          - "§11"
        depends_on:
          - "Implementation title"
```

## Notes
- phases represent implementation order (Phase 1 -> Phase 2)
- depends_on lists Issue titles from the same or previous phases
- files is a list of files to be modified (existing or new)
- spec_refs is a list of corresponding spec section numbers

## Submission Method
Save the proposal to a YAML file and submit with the following command:
```
{GOKRAX_CLI} spec suggestion-submit --pj {project} --reviewer {reviewer} --file <YAML file path>
```

[IMPORTANT] Complete proposal creation and submission without interruption."""
