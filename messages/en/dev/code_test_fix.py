"""Prompts for the CODE_TEST_FIX state.

Variables:
    project: str        - Project name
    test_output: str    - Test failure output (truncated)
    retry_count: int    - Current retry count (cumulative test failure count)
    max_retry: int      - Maximum retry count (MAX_TEST_RETRY)
    GOKRAX_CLI: str     - gokrax CLI path
"""


def cc_test_fix(
    project: str,
    test_output: str,
    retry_count: int,
    max_retry: int,
    **_kw: object,
) -> str:
    """Test fix prompt sent to CC."""
    return (
        f"Tests failed ({retry_count}/{max_retry}).\n"
        f"Read the test output below and fix the code to pass the tests.\n\n"
        f"```\n{test_output}\n```\n\n"
        f"After fixing, git commit.\n"
        f"Modifying test code is a last resort. Try fixing production code first.\n"
        f"Skipping tests or unconditionally updating snapshots is prohibited."
    )


def transition(
    project: str,
    test_output: str,
    retry_count: int,
    max_retry: int,
    GOKRAX_CLI: str,
    **_kw: object,
) -> str:
    """Test fix phase notification message for implementer."""
    return (
        f"Test fix phase ({retry_count}/{max_retry})\n"
        f"Tests failed. CC will attempt an automatic fix.\n\n"
        f"```\n{test_output[-2000:]}\n```"
    )


def nudge(**_kw: object) -> str:
    """CODE_TEST_FIX reminder."""
    return (
        "[Remind] Proceed with and complete the test fix work."
    )
