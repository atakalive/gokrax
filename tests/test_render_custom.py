import importlib
import os
import textwrap

import pytest

from messages import render


@pytest.fixture()
def custom_module(tmp_path, monkeypatch):
    """messages_custom/{lang}/{template}.py を tmp_path に作成し、
    sys.path に挿入してテスト後にクリーンアップするフィクスチャ。

    使い方:
        custom_module("ja", "dev.design_review", '''
            def phase_note(**kw):
                return "CUSTOM"
        ''')
    """
    import sys

    def _create(lang: str, template: str, code: str):
        # template "dev.design_review" -> "dev/design_review.py"
        parts = template.split(".")
        rel = os.path.join("messages_custom", lang, *parts[:-1], parts[-1] + ".py")
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(code))
        # tmp_path を sys.path 先頭に挿入（import 解決用）
        if str(tmp_path) not in sys.path:
            sys.path.insert(0, str(tmp_path))
        # importlib のキャッシュをクリア
        importlib.invalidate_caches()
        # 既にキャッシュされている場合はモジュールを削除
        mod_name = f"messages_custom.{lang}.{template}"
        sys.modules.pop(mod_name, None)

    yield _create

    # クリーンアップ: sys.path から tmp_path を除去 + モジュールキャッシュ削除
    import sys
    if str(tmp_path) in sys.path:
        sys.path.remove(str(tmp_path))
    to_remove = [k for k in sys.modules if k.startswith("messages_custom")]
    for k in to_remove:
        del sys.modules[k]
    importlib.invalidate_caches()


class TestRenderCustomOverride:
    """カスタムテンプレートによるオーバーライド機能のテスト。"""

    def test_custom_macro_overrides_default(self, custom_module):
        """カスタムモジュールに macro が存在すればそちらが使われる。"""
        custom_module("ja", "dev.design_review", '''
def phase_note(**kwargs):
    return "CUSTOM_OVERRIDE"
''')
        result = render("dev.design_review", "phase_note", lang="ja")
        assert result == "CUSTOM_OVERRIDE"

    def test_fallback_when_no_custom_module(self):
        """カスタムモジュールが存在しなければデフォルトが使われる。"""
        result = render("dev.design_review", "phase_note", lang="ja")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_fallback_when_macro_missing_in_custom(self, custom_module):
        """カスタムモジュールは存在するが、要求された macro が
        定義されていなければデフォルトにフォールバックする。"""
        custom_module("ja", "dev.design_review", '''
def some_other_function(**kwargs):
    return "OTHER"
''')
        # "phase_note" は custom にないのでデフォルトにフォールバック
        result = render("dev.design_review", "phase_note", lang="ja")
        assert isinstance(result, str)
        assert result != "OTHER"

    def test_custom_syntax_error_propagates(self, custom_module):
        """カスタムモジュールに SyntaxError があればそのまま例外が出る。"""
        custom_module("ja", "dev.design_review", '''
def phase_note(**kwargs)  # missing colon
    return "BAD"
''')
        with pytest.raises(SyntaxError):
            render("dev.design_review", "phase_note", lang="ja")

    def test_custom_internal_import_error_propagates(self, custom_module):
        """カスタムモジュール内部で存在しないライブラリを import している場合、
        ImportError が握りつぶされずにそのまま浮上すること。
        （find_spec 方式により、モジュール不在とモジュール内部エラーが区別される）"""
        custom_module("ja", "dev.design_review", '''
import non_existent_package_xyz

def phase_note(**kwargs):
    return "UNREACHABLE"
''')
        with pytest.raises(ModuleNotFoundError, match="non_existent_package_xyz"):
            render("dev.design_review", "phase_note", lang="ja")
