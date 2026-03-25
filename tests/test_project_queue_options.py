"""tests/test_project_queue_options.py — PROJECT_QUEUE_OPTIONS テスト (#231)"""

from config import resolve_queue_options
from task_queue import parse_queue_line


class TestProjectQueueOptions:
    """PJ別デフォルトキューオプションのテスト"""

    def test_pj_override_applied(self, monkeypatch):
        """PJ固有デフォルトが適用される"""
        monkeypatch.setattr("config.DEFAULT_QUEUE_OPTIONS", {"skip_test": True})
        monkeypatch.setattr("config.PROJECT_QUEUE_OPTIONS", {"ProjA": {"skip_test": False}})
        result = parse_queue_line("ProjA 1")
        assert result["skip_test"] is False

    def test_global_default_when_no_pj_config(self, monkeypatch):
        """PJ設定がない場合はグローバルデフォルト"""
        monkeypatch.setattr("config.DEFAULT_QUEUE_OPTIONS", {"skip_test": True})
        monkeypatch.setattr("config.PROJECT_QUEUE_OPTIONS", {})
        result = parse_queue_line("ProjB 1")
        assert result["skip_test"] is True

    def test_explicit_token_overrides_pj_default(self, monkeypatch):
        """明示トークンがPJ固有デフォルトを上書きする"""
        monkeypatch.setattr("config.DEFAULT_QUEUE_OPTIONS", {"skip_test": True})
        monkeypatch.setattr("config.PROJECT_QUEUE_OPTIONS", {"ProjA": {"skip_test": False}})
        result = parse_queue_line("ProjA 1 skip-test")
        assert result["skip_test"] is True

    def test_resolve_queue_options(self, monkeypatch):
        """resolve_queue_options ヘルパーの直接テスト"""
        monkeypatch.setattr("config.DEFAULT_QUEUE_OPTIONS", {"skip_test": True, "skip_assess": True})
        monkeypatch.setattr("config.PROJECT_QUEUE_OPTIONS", {"ProjA": {"skip_test": False}})
        assert resolve_queue_options("ProjA") == {"skip_test": False, "skip_assess": True}
        assert resolve_queue_options("Unknown") == {"skip_test": True, "skip_assess": True}

    def test_pj_only_key_not_in_global(self, monkeypatch):
        """PJ固有オプションのキーがグローバルにないケース"""
        monkeypatch.setattr("config.DEFAULT_QUEUE_OPTIONS", {})
        monkeypatch.setattr("config.PROJECT_QUEUE_OPTIONS", {"ProjA": {"skip_test": True}})
        result = parse_queue_line("ProjA 1")
        assert result["skip_test"] is True

    def test_pattern_a_key_pj_override(self, monkeypatch):
        """パターン A キーのPJ固有オーバーライド"""
        monkeypatch.setattr("config.DEFAULT_QUEUE_OPTIONS", {"impl=opus": True})
        monkeypatch.setattr("config.PROJECT_QUEUE_OPTIONS", {"ProjA": {"impl=sonnet": True}})
        result = parse_queue_line("ProjA 1")
        assert result["cc_impl_model"] == "sonnet"
