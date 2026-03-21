"""Prompts for the IMPLEMENTATION state (CC plan/impl/resume).

Generates prompt strings for CC execution.
Test baseline embedding logic remains in watchdog.py (due to subprocess dependency).

Variables:
    issues_block: str          - Issue body text (concatenation of ### #{N}: title\n{body})
    closes: str                - "Closes #1 Closes #2 ..." string
    comment_line: str          - Owner comment line (empty string or "{OWNER_NAME}'s request: ...\n\n")
    test_baseline_section: str - Test baseline section (empty string or "\n\n## Test Baseline...")
"""


def cc_plan(
    issues_block: str,
    closes: str,
    comment_line: str,
    **_kw,
) -> str:
    """CC Plan phase prompt."""
    return (
        f"Plan the implementation of the following Issues.\n"
        f"{comment_line}"
        f"\n{issues_block}\n\n"
        f"Include {closes} in the commit message.\n\n"
        f"After planning, output the implementation handoff in the following format:\n\n"
        f"## Implementation Handoff\n"
        f"### Files to Change\n"
        f"- File paths and changes (bulleted)\n\n"
        f"### Do Not Touch\n"
        f"- Existing code that must not be changed and why\n\n"
        f"### Pitfalls & Edge Cases\n"
        f"- Points to watch during implementation (all findings)\n\n"
        f"### Test Cases\n"
        f"- Cases to test (normal, error, boundary)"
    )


def cc_impl_resume(
    closes: str,
    scope_warning: str,
    test_baseline_section: str,
    **_kw,
) -> str:
    """CC Impl phase prompt (implementation instruction after Plan OK = resume session)."""
    return (
        f"Plan OK. Implement and commit. Include {closes} in the commit message."
        f"{scope_warning}"
        f"{test_baseline_section}"
    )


def cc_impl_skip_plan(
    issues_block: str,
    closes: str,
    comment_line: str,
    scope_warning: str,
    test_baseline_section: str,
    **_kw,
) -> str:
    """CC Impl phase prompt (Plan skip = direct implementation)."""
    return (
        f"Implement the following Issues.\n"
        f"{comment_line}"
        f"\n{issues_block}\n\n"
        f"Include {closes} in the commit message."
        f"{scope_warning}"
        f"{test_baseline_section}"
    )


def scope_warning_normal(**_kw) -> str:
    """Normal mode scope warning (Plan → Impl)."""
    return (
        "\n\n⚠️ Strict scope: Implement only the changes described in the Issue body."
        "Do not make any improvements, refactoring, or bug fixes not described in the Issue body."
    )


def scope_warning_skip_plan(**_kw) -> str:
    """skip_plan mode scope warning (direct Impl)."""
    return (
        "\n\n⚠️ Strict scope: Implement only the target files and changes described in the Issue body."
        "Never modify files listed under \"Do Not Touch\"."
        "Do not make any improvements, refactoring, or bug fixes not described in the Issue body."
    )


def test_baseline_pass(bl_output: str, **_kw) -> str:
    """Test baseline (all passed)."""
    return (
        "\n\n## Test Baseline (pre-implementation state)\n"
        f"exit_code: 0 (all passed)\n\n{bl_output}\n\n"
        "Do not break tests with your changes."
    )


def test_baseline_fail(bl_exit: int, bl_output: str, **_kw) -> str:
    """Test baseline (some failures)."""
    return (
        "\n\n## Test Baseline (pre-implementation state)\n"
        f"exit_code: {bl_exit} (some failures)\n\n{bl_output}\n\n"
        "⚠️ The above failures existed before implementation started.\n"
        "Do not introduce new failures with your changes."
    )


def cc_commit_retry(closes: str, **_kw) -> str:
    """CC commit retry instruction when no commit detected (in run_cc script)."""
    return (
        "Implementation is complete but not git committed. Run the following commands:\n\n"
        f'  git add -A\n'
        f'  git commit -m "feat({closes}): <summary of changes>"\n\n'
        f"Include {closes} in the commit message.\n"
        "If files to change are not in the working tree, re-read the Issue body's target files, implement, then commit."
    )


def nudge(**_kw) -> str:
    """IMPLEMENTATION reminder."""
    return (
        "[Remind] Proceed with and complete the implementation.\n"
        "Report the commit with gokrax commit --pj <project> --issue <N> --hash <commit>."
    )


# ---------------------------------------------------------------------------
# Discord notifications (CC progress, for _notify calls in bash scripts)
# ---------------------------------------------------------------------------

def notify_cc_plan_start(project: str, plan_model: str, q_tag: str = "", **_kw) -> str:
    """CC Plan started notification."""
    return f"{q_tag}[{project}] 📋 CC Plan started (model: {plan_model})"


def notify_cc_plan_done(project: str, q_tag: str = "", **_kw) -> str:
    """CC Plan completed notification."""
    return f"{q_tag}[{project}] ✅ CC Plan completed"


def notify_cc_impl_start(project: str, impl_model: str, q_tag: str = "", **_kw) -> str:
    """CC Impl started notification."""
    return f"{q_tag}[{project}] 🔨 CC Impl started (model: {impl_model})"


def notify_cc_impl_start_skip_plan(project: str, impl_model: str, q_tag: str = "", **_kw) -> str:
    """CC Impl started notification (plan skip)."""
    return f"{q_tag}[{project}] 🔨 CC Impl started (plan skip, model: {impl_model})"


def notify_cc_impl_done(project: str, q_tag: str = "", **_kw) -> str:
    """CC Impl completed notification."""
    return f"{q_tag}[{project}] ✅ CC Impl completed"


def notify_cc_no_commit_retry(project: str, retry: str, q_tag: str = "", **_kw) -> str:
    """CC no commit detected retry notification."""
    return f"{q_tag}[{project}] ⚠️ no commit detected — retrying CC ({retry})"


def notify_cc_no_commit_blocked(project: str, q_tag: str = "", **_kw) -> str:
    """CC commit creation failed → BLOCKED notification."""
    return f"{q_tag}[{project}] ❌ CC did not create a commit (after 2 retries) → BLOCKED"


def notify_cc_start_failed(project: str, error: str, **_kw) -> str:
    """CC startup failed notification."""
    return f"[{project}] ⚠️ CC startup failed: {error}"
