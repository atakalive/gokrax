"""Append new settings from settings.example.py to settings.py."""
from __future__ import annotations

import ast
import re
import shutil
import sys
from pathlib import Path

_COMMENTED_RE = re.compile(r"^#\s*([A-Z][A-Z0-9_]*)\s*(?::.*?)?\s*=", re.MULTILINE)


def _extract_var_names(source: str) -> set[str]:
    """Extract UPPER_CASE variable names from active assignments via AST."""
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


def _extract_commented_var_names(source: str) -> set[str]:
    """Extract UPPER_CASE variable names from commented-out assignments."""
    return set(_COMMENTED_RE.findall(source))


def _var_line_ranges(source: str) -> dict[str, tuple[int, int]]:
    """Map UPPER_CASE var name -> (start_line, end_line) 1-indexed inclusive."""
    tree = ast.parse(source)
    ranges: dict[str, tuple[int, int]] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    ranges[target.id] = (node.lineno, node.end_lineno or node.lineno)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id.isupper():
                ranges[node.target.id] = (node.lineno, node.end_lineno or node.lineno)
    return ranges


def main(base_dir: Path) -> int:
    """Run the update logic.  Return exit code (0=ok, 1=error)."""
    example_path = base_dir / "settings.example.py"
    settings_path = base_dir / "settings.py"

    if not settings_path.exists():
        shutil.copy2(example_path, settings_path)
        print("Created settings.py from settings.example.py")

    example_src = example_path.read_text(encoding="utf-8")
    settings_src = settings_path.read_text(encoding="utf-8")

    example_vars = _extract_var_names(example_src)
    settings_active = _extract_var_names(settings_src)
    settings_commented = _extract_commented_var_names(settings_src)
    existing = settings_active | settings_commented

    new_vars = sorted(example_vars - existing)
    if not new_vars:
        print("settings.py is up to date.")
    else:
        example_lines = example_src.splitlines()
        ranges = _var_line_ranges(example_src)

        chunks: list[str] = []
        for var in new_vars:
            start, end = ranges[var]
            block = example_lines[start - 1:end]
            commented = ["# " + line for line in block]
            commented[-1] += "  # NEW"
            chunks.append("\n".join(commented))

        appendix = "\n# --- New settings (added by update_settings.py) ---\n"
        appendix += "\n".join(chunks) + "\n"

        with open(settings_path, "a", encoding="utf-8") as f:
            f.write(appendix)

        print(f"Added {len(new_vars)} new setting(s): {', '.join(new_vars)}")

    queue_path = base_dir / "gokrax-queue.txt"
    template_path = base_dir / "gokrax-queue.example.txt"
    if not queue_path.exists():
        if not template_path.exists():
            print("gokrax-queue.example.txt not found, skipping queue file creation.", file=sys.stderr)
        else:
            shutil.copy2(template_path, queue_path)
            print("Created gokrax-queue.txt from template.")

    pi_config_path = base_dir / "agents" / "config_pi.json"
    pi_example_path = base_dir / "agents" / "config_pi.example.json"
    if not pi_config_path.exists():
        if not pi_example_path.exists():
            print("agents/config_pi.example.json not found, skipping.", file=sys.stderr)
        else:
            shutil.copy2(pi_example_path, pi_config_path)
            print("Created agents/config_pi.json from template.")

    return 0


if __name__ == "__main__":
    sys.exit(main(Path(__file__).resolve().parent))
