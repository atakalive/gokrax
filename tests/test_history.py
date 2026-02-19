"""tests/test_history.py — history 肥大化対策テスト（MAX_HISTORY 切り詰め）"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestMaxHistoryConstant:

    def test_max_history_exists(self):
        """MAX_HISTORY が config に定義されていること"""
        import config
        assert hasattr(config, "MAX_HISTORY")
        assert config.MAX_HISTORY == 100


class TestHistoryTruncation:

    def test_under_limit_no_truncation(self):
        """history が MAX_HISTORY 以下 → 全件保持"""
        from pipeline_io import add_history
        data = {"history": []}
        for i in range(5):
            add_history(data, "A", "B", actor=f"actor_{i}")
        assert len(data["history"]) == 5

    def test_over_limit_truncated_to_max(self):
        """history が MAX_HISTORY を超過 → 最新 MAX_HISTORY 件のみ残る"""
        from pipeline_io import add_history
        import config
        data = {"history": [
            {"from": "A", "to": "B", "at": f"t{i}", "actor": f"old_{i}"}
            for i in range(config.MAX_HISTORY)
        ]}
        add_history(data, "X", "Y", actor="newest")
        assert len(data["history"]) == config.MAX_HISTORY
        assert data["history"][-1]["actor"] == "newest"
        assert data["history"][-1]["from"] == "X"
        # 最古のエントリ（old_0）が除去されていること
        actors = [h["actor"] for h in data["history"]]
        assert "old_0" not in actors

    def test_empty_history_first_entry(self):
        """空 history から追加 → 正常に1件追加"""
        from pipeline_io import add_history
        data = {}
        add_history(data, "IDLE", "DESIGN_PLAN", actor="cli")
        assert len(data["history"]) == 1
        assert data["history"][0]["from"] == "IDLE"
        assert data["history"][0]["to"] == "DESIGN_PLAN"

    def test_large_overflow_truncated(self):
        """大幅超過 → 正確に MAX_HISTORY 件に切り詰め"""
        from pipeline_io import add_history
        import config
        data = {"history": [
            {"from": "A", "to": "B", "at": f"t{i}", "actor": f"a{i}"}
            for i in range(config.MAX_HISTORY + 50)
        ]}
        add_history(data, "Z", "W", actor="final")
        assert len(data["history"]) == config.MAX_HISTORY
        assert data["history"][-1]["actor"] == "final"
