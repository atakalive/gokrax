"""Prompts and reminders for the ASSESSMENT state.

Variables:
    project: str       - Project name
    issues_str: str    - Target Issue numbers string (e.g. "#1, #2")
    comment_line: str  - Owner comment line (empty string or "{OWNER_NAME}'s request: ...\n")
    GOKRAX_CLI: str    - gokrax CLI path
"""


def transition(
    project: str,
    issues_str: str,
    comment_line: str,
    GOKRAX_CLI: str,
    **_kw,
) -> str:
    """ASSESSMENT phase instruction message."""
    return (
        f"[gokrax] {project}: assessment phase\n"
        f"{comment_line}"
        f"Target Issues: {issues_str}\n"
        f"Assess the overall batch difficulty level (Lvl 1-5) using the criteria below, then run assess-done.\n"
        f"\n"
        f"Criteria (code complexity):\n"
        f"  Lvl 1: Single file, routine change (constant change, text fix, etc.)\n"
        f"  Lvl 2: A few files, following existing patterns (new option, etc.)\n"
        f"  Lvl 3: Multiple modules, new logic addition\n"
        f"  Lvl 4: Large-scale rewrite, impacts multiple existing flows\n"
        f"  Lvl 5: Structural change affecting the entire codebase\n"
        f"\n"
        f"{GOKRAX_CLI} assess-done --project {project} --complex-level N --summary \"reason\"\n"
        f"[Request] Complete the work without interruption."
    )


def nudge(
    **_kw,
) -> str:
    """ASSESSMENT reminder."""
    return (
        "[Remind] Assess the difficulty and run assess-done.\n"
        "gokrax assess-done --project <project> --complex-level N --summary \"reason\""
    )
