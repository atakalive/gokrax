"""tests/test_implementers_guard.py — IMPLEMENTERS 空リストガードと argparse default テスト"""

import importlib
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestImplementersEmptyGuard:
    """IMPLEMENTERS が空リストの場合、config バリデーションで RuntimeError が発生する。"""

    def test_empty_implementers_raises_runtime_error(self, tmp_path, monkeypatch):
        """IMPLEMENTERS=[] かつ REVIEWERS が存在する設定で config を reload すると、
        IMPLEMENTERS 単独ガードの RuntimeError が発生する。"""
        # 一時 settings.py を作成: REVIEWERS あり、IMPLEMENTERS 空
        settings_file = tmp_path / "settings.py"
        settings_file.write_text(
            'REVIEWERS = ["reviewer1", "reviewer2"]\n'
            'IMPLEMENTERS = []\n'
            'REVIEW_MODES = {"standard": {"members": ["reviewer1", "reviewer2"], "min_reviews": 1}}\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("GOKRAX_SETTINGS", str(settings_file))
        # GOKRAX_SKIP_USER_SETTINGS を解除して settings を読ませる
        monkeypatch.delenv("GOKRAX_SKIP_USER_SETTINGS", raising=False)

        import config
        with pytest.raises(RuntimeError, match="IMPLEMENTERS is empty"):
            importlib.reload(config)

    def test_guard_exists_in_config_source(self):
        """config/__init__.py にガードコードが存在することをソースで確認する。"""
        source = (ROOT / "config" / "__init__.py").read_text(encoding="utf-8")
        assert "if not IMPLEMENTERS:" in source
        assert "IMPLEMENTERS is empty" in source


class TestArgparseImplementerDefault:
    """gokrax init の --implementer が IMPLEMENTERS[0] を default に使い、
    help 文字列が %(default)s で自動反映されることを確認する。"""

    def test_argparse_uses_implementers_0_not_hardcoded(self):
        """gokrax.py の --implementer 行が IMPLEMENTERS[0] を参照し、
        ハードコードされた実装者名を含まないことをソースで確認する。"""
        source = (ROOT / "gokrax.py").read_text(encoding="utf-8")
        # --implementer の add_argument 行を抽出
        lines = [line for line in source.splitlines()
                 if "--implementer" in line and "add_argument" in line]
        assert len(lines) >= 1, "--implementer add_argument line not found"
        line = lines[0]
        # default が IMPLEMENTERS[0] であること
        assert "IMPLEMENTERS[0]" in line, (
            f"Expected default=IMPLEMENTERS[0], got: {line.strip()}"
        )
        # help に %(default)s が使われていること
        assert "%(default)s" in line, (
            f"Expected help to use %(default)s, got: {line.strip()}"
        )

    def test_no_hardcoded_implementer_in_production_code(self):
        """本番コード内にハードコードされた実装者名フォールバックが残っていないことを確認する。
        settings.py の設定値定義は除く。"""
        prod_files = [
            "gokrax.py",
            "watchdog.py",
            "commands/dev.py",
            "commands/spec.py",
        ]
        for fname in prod_files:
            source = (ROOT / fname).read_text(encoding="utf-8")
            # "kaneko" がフォールバック値として使われていないことを確認
            # data.get("implementer", "kaneko") や or "kaneko" のパターンを検出
            matches = re.findall(r'''(?:,\s*["']kaneko["']|or\s+["']kaneko["'])''', source)
            assert not matches, (
                f"{fname} にハードコードされた実装者名フォールバックが残っている: {matches}"
            )
