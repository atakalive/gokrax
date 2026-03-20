"""Tests ensuring settings.example.py stays consistent with config."""
from __future__ import annotations

import ast
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLE_PATH = _PROJECT_ROOT / "settings.example.py"

# Variables in settings.example.py that use placeholder values
_PLACEHOLDER_VARS = frozenset({
    "DISCORD_CHANNEL",
    "DISCORD_BOT_TOKEN",
    "MERGE_APPROVER_DISCORD_ID",
    "ANNOUNCE_BOT_USER_ID",
    "COMMAND_BOT_USER_ID",
    "AGENTS",
    "GOKRAX_CLI",
    "GLAB_BIN",
    "OWNER_NAME",
    "PROMPT_LANG",
    "REVIEWER_TIERS",
    "REVIEW_MODES",
    "TEST_CONFIG",
    "PIPELINES_DIR",
    "LOCAL_TZ",
})


def _parse_example_var_names() -> set[str]:
    """Parse settings.example.py and return active UPPER_CASE var names."""
    source = _EXAMPLE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id.isupper():
                names.add(node.target.id)
    return names


def test_example_vars_subset_of_config() -> None:
    import config

    example_vars = _parse_example_var_names()
    config_vars = {k for k in dir(config) if k.isupper() and not k.startswith("_")}

    missing = example_vars - config_vars
    assert not missing, f"settings.example.py has vars not in config: {missing}"


def test_example_defaults_match_config() -> None:
    import config

    source = _EXAMPLE_PATH.read_text(encoding="utf-8")
    # Execute settings.example.py in isolation to get its values
    example_ns: dict = {}
    exec(compile(source, str(_EXAMPLE_PATH), "exec"), example_ns)  # noqa: S102

    example_vars = _parse_example_var_names()

    for var in sorted(example_vars - _PLACEHOLDER_VARS):
        example_val = example_ns.get(var)
        config_val = getattr(config, var, None)
        assert example_val == config_val, (
            f"{var}: example={example_val!r} != config={config_val!r}"
        )
