"""tests/test_load_skills.py — フェーズ別・PJ別スキル注入のテスト (Issue #174)"""

import logging

import config
import notify


class TestLoadSkillsPhase:
    """load_skills のフェーズ別・PJ別スキル注入テスト。"""

    def test_phase_filter(self, tmp_path: object, monkeypatch: object) -> None:
        """AGENT_SKILLS のフェーズ別フィルタが正しく動作すること。"""
        s1 = tmp_path / "s1.md"
        s1.write_text("Skill S1 content", encoding="utf-8")
        s2 = tmp_path / "s2.md"
        s2.write_text("Skill S2 content", encoding="utf-8")

        monkeypatch.setattr(config, "SKILLS", {
            "s1": str(s1),
            "s2": str(s2),
        })
        monkeypatch.setattr(config, "AGENT_SKILLS", {
            "r1": {"design": ["s1"], "code": ["s2"]},
        })
        monkeypatch.setattr(config, "PROJECT_SKILLS", {})

        result = notify.load_skills("r1", "", "design")
        assert "s1" in result
        assert "s2" not in result

        result = notify.load_skills("r1", "", "code")
        assert "s2" in result
        assert "s1" not in result

    def test_project_skills_union(self, tmp_path: object, monkeypatch: object) -> None:
        """AGENT_SKILLS と PROJECT_SKILLS の和集合が返ること。"""
        s1 = tmp_path / "s1.md"
        s1.write_text("Skill S1 content", encoding="utf-8")
        s2 = tmp_path / "s2.md"
        s2.write_text("Skill S2 content", encoding="utf-8")

        monkeypatch.setattr(config, "SKILLS", {
            "s1": str(s1),
            "s2": str(s2),
        })
        monkeypatch.setattr(config, "AGENT_SKILLS", {
            "r1": {"code": ["s1"]},
        })
        monkeypatch.setattr(config, "PROJECT_SKILLS", {
            "pj": {"code": ["s2"]},
        })

        result = notify.load_skills("r1", "pj", "code")
        assert "s1" in result
        assert "s2" in result

    def test_dedup(self, tmp_path: object, monkeypatch: object) -> None:
        """AGENT_SKILLS と PROJECT_SKILLS に同じスキル名がある場合、重複排除されること。"""
        s1 = tmp_path / "s1.md"
        s1.write_text("Skill S1 content", encoding="utf-8")

        monkeypatch.setattr(config, "SKILLS", {
            "s1": str(s1),
        })
        monkeypatch.setattr(config, "AGENT_SKILLS", {
            "r1": {"code": ["s1"]},
        })
        monkeypatch.setattr(config, "PROJECT_SKILLS", {
            "pj": {"code": ["s1"]},
        })

        result = notify.load_skills("r1", "pj", "code")
        # スキルブロック内に s1 のセクションが 1 回だけ含まれること
        assert result.count("--- skill: s1 ---") == 1

    def test_empty_phase(self, monkeypatch: object) -> None:
        """phase が空文字列の場合、スキル注入なし（空文字列が返る）。"""
        monkeypatch.setattr(config, "AGENT_SKILLS", {
            "r1": {"code": ["s1"]},
        })
        monkeypatch.setattr(config, "PROJECT_SKILLS", {})

        result = notify.load_skills("r1", "pj", "")
        assert result == ""

    def test_legacy_list_fallback(self, tmp_path: object, monkeypatch: object) -> None:
        """旧形式 list[str] が全フェーズに適用されること。"""
        s1 = tmp_path / "s1.md"
        s1.write_text("Skill S1 content", encoding="utf-8")

        monkeypatch.setattr(config, "SKILLS", {"s1": str(s1)})
        monkeypatch.setattr(config, "AGENT_SKILLS", {"r1": ["s1"]})
        monkeypatch.setattr(config, "PROJECT_SKILLS", {})

        result = notify.load_skills("r1", "", "code")
        assert "s1" in result
        assert "Skill S1 content" in result

        result = notify.load_skills("r1", "", "design")
        assert "s1" in result

    def test_legacy_list_warning(self, tmp_path: object, monkeypatch: object, caplog: object) -> None:
        """旧形式 list[str] 使用時に deprecation warning が出力されること。"""
        s1 = tmp_path / "s1.md"
        s1.write_text("Skill S1 content", encoding="utf-8")

        monkeypatch.setattr(config, "SKILLS", {"s1": str(s1)})
        monkeypatch.setattr(config, "AGENT_SKILLS", {"r1": ["s1"]})
        monkeypatch.setattr(config, "PROJECT_SKILLS", {})

        with caplog.at_level(logging.WARNING, logger="gokrax.notify"):
            notify.load_skills("r1", "", "code")

        assert "deprecated" in caplog.text.lower()
