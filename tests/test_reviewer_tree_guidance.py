"""tests/test_reviewer_tree_guidance.py — レビュー観点テキストのツリー制約 (Issue #395)

レビュアーが実装者と共有する作業ツリーを `git checkout` 等で書き換えると、
detached HEAD のまま実装者のコミットが積まれ DONE 時の push が失敗する。
禁止文が design/code 両フェーズ・ja/en 両言語の guidance に載ることを保証する。

`render()` ではなく基底モジュールを直接 import する: render() は
messages_custom/ の同名マクロを優先する仕様で、custom はマシンローカル
（git 管理外）のため、render() 経由だと検証結果が環境に依存する。
ここで保証するのは gokrax が配布する基底テンプレートの内容。
"""

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

CASES = [
    ("ja", "code_review", "guidance_code"),
    ("ja", "design_review", "guidance_design"),
    ("en", "code_review", "guidance_code"),
    ("en", "design_review", "guidance_design"),
]


@pytest.mark.parametrize(("lang", "module", "macro"), CASES)
def test_guidance_forbids_tree_mutating_git(lang: str, module: str, macro: str) -> None:
    mod = importlib.import_module(f"messages.{lang}.dev.{module}")
    text = getattr(mod, macro)()
    for verb in ("checkout", "switch", "stash", "restore", "reset", "clean"):
        assert verb in text, f"{lang}/{module}: {verb!r} missing from guidance"
