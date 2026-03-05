"""tests/test_queue.py — queue.py のテスト"""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
import tempfile

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from task_queue import (
    parse_queue_line, pop_next_queue_entry, restore_queue_entry, peek_queue,
    get_active_entries, append_entry, delete_entry,
)


class TestParseQueueLine:
    """parse_queue_line() のテスト"""

    def test_valid_all_options(self):
        """全オプション指定"""
        line = "TrajOpt 25 standard automerge plan=opus impl=sonnet"
        result = parse_queue_line(line)
        assert result is not None
        assert result["project"] == "TrajOpt"
        assert result["issues"] == "25"
        assert result["mode"] == "standard"
        assert result["automerge"] is True
        assert result["cc_plan_model"] == "opus"
        assert result["cc_impl_model"] == "sonnet"
        assert result["original_line"] == line

    def test_valid_minimal(self):
        """最小形式 (PROJECT ISSUES のみ)"""
        line = "BeamShifter all"
        result = parse_queue_line(line)
        assert result is not None
        assert result["project"] == "BeamShifter"
        assert result["issues"] == "all"
        assert result["mode"] is None
        assert result["automerge"] is False
        assert result["keep_ctx_batch"] is False
        assert result["keep_ctx_intra"] is False
        assert result["cc_plan_model"] is None
        assert result["cc_impl_model"] is None

    def test_valid_comma_separated_issues(self):
        """カンマ区切り issues"""
        line = "Foo 1,2,3 lite"
        result = parse_queue_line(line)
        assert result is not None
        assert result["issues"] == "1,2,3"

    def test_invalid_issues_empty_element(self):
        """issues に空要素 → ValueError"""
        with pytest.raises(ValueError, match="empty element"):
            parse_queue_line("Foo 1,,3")

    def test_invalid_issues_non_digit(self):
        """issues に非数値 → ValueError"""
        with pytest.raises(ValueError, match="non-integer"):
            parse_queue_line("Foo 1,abc")

    def test_invalid_issues_spaces(self):
        """issues にスペース混入 → ValueError (トークン分割で別扱い)"""
        with pytest.raises(ValueError):
            parse_queue_line("Foo 1, 2, 3")

    def test_empty_line(self):
        """空行 → ValueError"""
        with pytest.raises(ValueError):
            parse_queue_line("")
        with pytest.raises(ValueError):
            parse_queue_line("   ")

    def test_comment_line(self):
        """コメント行 → ValueError"""
        with pytest.raises(ValueError):
            parse_queue_line("# comment")
        with pytest.raises(ValueError):
            parse_queue_line("# done: Foo 1")

    def test_invalid_token_count(self):
        """トークン数不足 → ValueError"""
        with pytest.raises(ValueError, match="need PROJECT ISSUES"):
            parse_queue_line("OnlyProject")

    def test_duplicate_mode(self):
        """MODE 重複 → ValueError"""
        with pytest.raises(ValueError, match="Duplicate mode"):
            parse_queue_line("Foo 1 standard lite")

    def test_unknown_token(self):
        """不明トークン → ValueError"""
        with pytest.raises(ValueError, match="Unknown token"):
            parse_queue_line("Foo 1 unknown_option")

    def test_valid_automerge_only(self):
        """automerge のみ"""
        line = "Foo 1 automerge"
        result = parse_queue_line(line)
        assert result is not None
        assert result["automerge"] is True
        assert result["mode"] is None

    def test_valid_plan_model_only(self):
        """plan=MODEL のみ"""
        line = "Foo 1 plan=opus"
        result = parse_queue_line(line)
        assert result is not None
        assert result["cc_plan_model"] == "opus"
        assert result["cc_impl_model"] is None

    def test_valid_mode_and_options(self):
        """MODE + OPTIONS"""
        line = "Foo 1 full automerge plan=haiku impl=sonnet"
        result = parse_queue_line(line)
        assert result is not None
        assert result["mode"] == "full"
        assert result["automerge"] is True
        assert result["cc_plan_model"] == "haiku"
        assert result["cc_impl_model"] == "sonnet"

    # --- keep-ctx tests (Issue #58) ---

    def test_keep_ctx_batch_only(self):
        """keep-ctx-batch 単独"""
        result = parse_queue_line("Foo 1 keep-ctx-batch")
        assert result["keep_ctx_batch"] is True
        assert result["keep_ctx_intra"] is False

    def test_keep_ctx_intra_only(self):
        """keep-ctx-intra 単独"""
        result = parse_queue_line("Foo 1 keep-ctx-intra")
        assert result["keep_ctx_batch"] is False
        assert result["keep_ctx_intra"] is True

    def test_keep_ctx_all(self):
        """keep-ctx-all → 両方True"""
        result = parse_queue_line("Foo 1 keep-ctx-all")
        assert result["keep_ctx_batch"] is True
        assert result["keep_ctx_intra"] is True

    def test_keep_context_legacy(self):
        """keep-context (後方互換) → 両方True"""
        result = parse_queue_line("Foo 1 keep-context")
        assert result["keep_ctx_batch"] is True
        assert result["keep_ctx_intra"] is True

    def test_keep_ctx_batch_and_intra_separate(self):
        """keep-ctx-batch + keep-ctx-intra 個別指定 → 両方True"""
        result = parse_queue_line("Foo 1 keep-ctx-batch keep-ctx-intra")
        assert result["keep_ctx_batch"] is True
        assert result["keep_ctx_intra"] is True

    def test_no_keep_ctx_flags(self):
        """フラグなし → 両方False"""
        result = parse_queue_line("Foo 1")
        assert result["keep_ctx_batch"] is False
        assert result["keep_ctx_intra"] is False

    def test_p1_fix(self):
        """p1-fix トークン"""
        result = parse_queue_line("Foo 1 p1-fix")
        assert result["p1_fix"] is True

    def test_p1_fix_with_mode(self):
        """p1-fix + MODE + 他オプション"""
        result = parse_queue_line("BeamShifter 43,44 full p1-fix automerge")
        assert result["p1_fix"] is True
        assert result["mode"] == "full"
        assert result["automerge"] is True

    def test_no_p1_fix_default(self):
        """p1-fix 省略時は False"""
        result = parse_queue_line("Foo 1")
        assert result["p1_fix"] is False


class TestPopNextQueueEntry:
    """pop_next_queue_entry() のテスト"""

    def test_pop_idle_project(self, tmp_path, monkeypatch):
        """IDLE プロジェクトをpop"""
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("ProjA 1\n")

        # Mock: ProjA は IDLE
        def mock_get_path(project):
            return tmp_path / f"{project}.json"

        def mock_load_pipeline(path):
            if "ProjA" in str(path):
                return {"state": "IDLE"}
            raise FileNotFoundError

        monkeypatch.setattr("task_queue.get_path", mock_get_path)
        monkeypatch.setattr("task_queue.load_pipeline", mock_load_pipeline)

        # Pipeline ファイルを作成
        (tmp_path / "ProjA.json").write_text('{"state": "IDLE"}')

        entry = pop_next_queue_entry(queue_file)
        assert entry is not None
        assert entry["project"] == "ProjA"
        assert entry["issues"] == "1"

        # ファイルが "# done:" で書き換えられている
        content = queue_file.read_text()
        assert "# done: ProjA 1" in content

    def test_skip_non_idle_project(self, tmp_path, monkeypatch):
        """非IDLE プロジェクトをスキップして次行をpop"""
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("ProjA 1\nProjB 2\n")

        def mock_get_path(project):
            return tmp_path / f"{project}.json"

        def mock_load_pipeline(path):
            if "ProjA" in str(path):
                return {"state": "DESIGN_PLAN"}  # Not IDLE
            if "ProjB" in str(path):
                return {"state": "IDLE"}
            raise FileNotFoundError

        monkeypatch.setattr("task_queue.get_path", mock_get_path)
        monkeypatch.setattr("task_queue.load_pipeline", mock_load_pipeline)

        # Pipeline ファイル作成
        (tmp_path / "ProjA.json").write_text('{"state": "DESIGN_PLAN"}')
        (tmp_path / "ProjB.json").write_text('{"state": "IDLE"}')

        entry = pop_next_queue_entry(queue_file)
        assert entry is not None
        assert entry["project"] == "ProjB"

        # ProjA はスキップされ、ProjB のみ done 化
        content = queue_file.read_text()
        assert "ProjA 1\n" in content  # Not marked as done
        assert "# done: ProjB 2" in content

    def test_empty_queue(self, tmp_path):
        """空キュー → None"""
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("")
        assert pop_next_queue_entry(queue_file) is None

    def test_queue_file_not_exist(self, tmp_path):
        """キューファイル不存在 → None"""
        queue_file = tmp_path / "nonexistent.txt"
        assert pop_next_queue_entry(queue_file) is None

    def test_all_lines_invalid(self, tmp_path):
        """全行が無効 → None"""
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("# comment\n\ninvalid\n")
        assert pop_next_queue_entry(queue_file) is None

    def test_all_lines_done(self, tmp_path):
        """全行が done 済み → None"""
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("# done: ProjA 1\n# done: ProjB 2\n")
        assert pop_next_queue_entry(queue_file) is None

    def test_pipeline_not_found(self, tmp_path, monkeypatch):
        """Pipeline ファイル不存在 → スキップして None"""
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("ProjA 1\n")

        def mock_get_path(project):
            return tmp_path / f"{project}.json"

        monkeypatch.setattr("task_queue.get_path", mock_get_path)

        # Pipeline ファイルを作らない → FileNotFoundError
        entry = pop_next_queue_entry(queue_file)
        assert entry is None


class TestRestoreQueueEntry:
    """restore_queue_entry() のテスト"""

    def test_restore_done_line(self, tmp_path):
        """done 行を復元"""
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("# done: Foo 1\nBar 2\n")

        result = restore_queue_entry(queue_file, "Foo 1")
        assert result is True

        content = queue_file.read_text()
        assert "Foo 1\n" in content
        assert "# done: Foo 1" not in content
        assert "Bar 2\n" in content

    def test_restore_not_found(self, tmp_path):
        """該当行なし → False"""
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("# done: Bar 2\n")

        result = restore_queue_entry(queue_file, "Foo 1")
        assert result is False

    def test_restore_queue_file_not_exist(self, tmp_path):
        """キューファイル不存在 → False"""
        queue_file = tmp_path / "nonexistent.txt"
        result = restore_queue_entry(queue_file, "Foo 1")
        assert result is False


class TestPeekQueue:
    """peek_queue() のテスト"""

    def test_peek_all_entries(self, tmp_path):
        """全エントリを返す (done フラグ付き)"""
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("Foo 1\n# done: Bar 2\nBaz 3 automerge\n")

        entries = peek_queue(queue_file)
        assert len(entries) == 3
        assert entries[0]["project"] == "Foo"
        assert entries[0]["done"] is False
        assert entries[1]["project"] == "Bar"
        assert entries[1]["done"] is True
        assert entries[2]["project"] == "Baz"
        assert entries[2]["done"] is False
        assert entries[2]["automerge"] is True

    def test_peek_empty_queue(self, tmp_path):
        """空キュー → []"""
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("")
        assert peek_queue(queue_file) == []

    def test_peek_queue_not_exist(self, tmp_path):
        """キューファイル不存在 → []"""
        queue_file = tmp_path / "nonexistent.txt"
        assert peek_queue(queue_file) == []

    def test_peek_skips_invalid_lines(self, tmp_path):
        """無効行はスキップ"""
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("Foo 1\n# comment\ninvalid\nBar 2\n")

        entries = peek_queue(queue_file)
        assert len(entries) == 2
        assert entries[0]["project"] == "Foo"
        assert entries[1]["project"] == "Bar"


class TestGetActiveEntries:
    """get_active_entries() のテスト"""

    def test_filters_done_entries(self, tmp_path):
        """done 行を除外し、index が 0 始まり連番"""
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("Foo 1\n# done: Bar 2\nBaz 3 automerge\n")

        entries = get_active_entries(queue_file)
        assert len(entries) == 2
        assert entries[0]["project"] == "Foo"
        assert entries[0]["index"] == 0
        assert entries[1]["project"] == "Baz"
        assert entries[1]["index"] == 1
        assert entries[1]["automerge"] is True

    def test_empty_queue(self, tmp_path):
        """空キュー → 空リスト"""
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("")
        assert get_active_entries(queue_file) == []

    def test_all_done(self, tmp_path):
        """全行 done → 空リスト"""
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("# done: Foo 1\n# done: Bar 2\n")
        assert get_active_entries(queue_file) == []

    def test_file_not_exist(self, tmp_path):
        """ファイル不存在 → 空リスト"""
        queue_file = tmp_path / "nonexistent.txt"
        assert get_active_entries(queue_file) == []


class TestAppendEntry:
    """append_entry() のテスト"""

    def test_append_normal(self, tmp_path):
        """正常ケース: 行が末尾に追加される"""
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("Foo 1\n")

        result = append_entry(queue_file, "Bar 2 automerge")
        assert result["project"] == "Bar"
        assert result["issues"] == "2"
        assert result["automerge"] is True

        content = queue_file.read_text()
        assert content == "Foo 1\nBar 2 automerge\n"

    def test_append_validation_error(self, tmp_path):
        """不正行で ValueError"""
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("Foo 1\n")

        with pytest.raises(ValueError):
            append_entry(queue_file, "# comment")

        # ファイル内容が変わっていないこと
        assert queue_file.read_text() == "Foo 1\n"

    def test_append_no_trailing_newline(self, tmp_path):
        """末尾改行なしファイルでも改行が補われる"""
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("Foo 1")  # no trailing newline

        append_entry(queue_file, "Bar 2")
        content = queue_file.read_text()
        assert content == "Foo 1\nBar 2\n"

    def test_append_file_not_exist(self, tmp_path):
        """ファイル不存在 → FileNotFoundError"""
        queue_file = tmp_path / "nonexistent.txt"
        with pytest.raises(FileNotFoundError):
            append_entry(queue_file, "Foo 1")


class TestDeleteEntry:
    """delete_entry() のテスト"""

    def test_delete_first(self, tmp_path):
        """index=0 で先頭 active 行を削除"""
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("Foo 1\nBar 2\nBaz 3\n")

        result = delete_entry(queue_file, 0)
        assert result is not None
        assert result["project"] == "Foo"

        content = queue_file.read_text()
        assert "Foo 1" not in content
        assert "Bar 2\n" in content
        assert "Baz 3\n" in content

    def test_delete_last_keyword(self, tmp_path):
        """index='last' で最後の active 行を削除"""
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("Foo 1\nBar 2\nBaz 3\n")

        result = delete_entry(queue_file, "last")
        assert result is not None
        assert result["project"] == "Baz"

        content = queue_file.read_text()
        assert "Foo 1\n" in content
        assert "Bar 2\n" in content
        assert "Baz 3" not in content

    def test_delete_minus_one(self, tmp_path):
        """index='-1' で最後の active 行を削除"""
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("Foo 1\nBar 2\n")

        result = delete_entry(queue_file, "-1")
        assert result is not None
        assert result["project"] == "Bar"

    def test_delete_out_of_range(self, tmp_path):
        """範囲外 → None"""
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("Foo 1\n")

        assert delete_entry(queue_file, 5) is None

    def test_delete_empty_queue(self, tmp_path):
        """空キュー → None"""
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("")
        assert delete_entry(queue_file, 0) is None

    def test_delete_skips_done_lines(self, tmp_path):
        """done 行はインデックス対象外"""
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("# done: Foo 1\nBar 2\nBaz 3\n")

        # index=0 は Bar (Foo は done なのでスキップ)
        result = delete_entry(queue_file, 0)
        assert result is not None
        assert result["project"] == "Bar"

        content = queue_file.read_text()
        assert "# done: Foo 1\n" in content
        assert "Baz 3\n" in content

    def test_delete_file_not_exist(self, tmp_path):
        """ファイル不存在 → None"""
        queue_file = tmp_path / "nonexistent.txt"
        assert delete_entry(queue_file, 0) is None

    def test_delete_entry_index_matches_get_active(self, tmp_path):
        """get_active_entries のインデックスと delete_entry のインデックスが一致する。

        done行・コメント行・空行が混在するキューで、共通化後の整合性を検証する。
        """
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text(
            "# done: Foo 1\n"
            "# このコメントは無視される\n"
            "\n"
            "Bar 2\n"
            "# done: Baz 3\n"
            "Qux 4\n"
        )

        active = get_active_entries(queue_file)
        assert len(active) == 2
        assert active[0]["project"] == "Bar"
        assert active[0]["index"] == 0
        assert active[1]["project"] == "Qux"
        assert active[1]["index"] == 1

        # get_active_entries の index=0 → delete_entry の index=0 が Bar を削除
        result = delete_entry(queue_file, 0)
        assert result is not None
        assert result["project"] == "Bar"

        content = queue_file.read_text()
        assert "Bar 2" not in content
        assert "Qux 4\n" in content
        assert "# done: Foo 1\n" in content
