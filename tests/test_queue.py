"""tests/test_queue.py — queue.py のテスト"""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
import tempfile

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import task_queue
from task_queue import (
    parse_queue_line, pop_next_queue_entry, restore_queue_entry, peek_queue,
    get_active_entries, append_entry, delete_entry, replace_entry, sanitize_comment,
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
        assert result["automerge"] is True
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

    def test_no_automerge_token(self):
        """no-automerge トークン → automerge=False"""
        result = parse_queue_line("Foo 1 no-automerge")
        assert result is not None
        assert result["automerge"] is False
        assert result["mode"] is None

    def test_automerge_and_no_automerge_conflict(self):
        """automerge + no-automerge 同時指定 → ValueError"""
        with pytest.raises(ValueError, match="cannot be used together"):
            parse_queue_line("Foo 1 automerge no-automerge")
        with pytest.raises(ValueError, match="cannot be used together"):
            parse_queue_line("Foo 1 no-automerge automerge")

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

    def test_p2_fix(self):
        """p2-fix トークン"""
        result = parse_queue_line("Foo 1 p2-fix")
        assert result["p2_fix"] is True

    def test_p2_fix_with_mode(self):
        """p2-fix + MODE + 他オプション"""
        result = parse_queue_line("BeamShifter 43,44 full p2-fix automerge")
        assert result["p2_fix"] is True
        assert result["mode"] == "full"
        assert result["automerge"] is True

    def test_no_p2_fix_default(self):
        """p2-fix 省略時は False"""
        result = parse_queue_line("Foo 1")
        assert result["p2_fix"] is False

    def test_skip_cc_plan(self):
        """skip-cc-plan トークン → skip_cc_plan=True"""
        result = parse_queue_line("Foo 1 skip-cc-plan")
        assert result["skip_cc_plan"] is True

    def test_skip_cc_plan_with_other_options(self):
        """skip-cc-plan + automerge + mode の組み合わせ"""
        result = parse_queue_line("EMCalibrator 64,65 automerge skip-cc-plan")
        assert result["skip_cc_plan"] is True
        assert result["automerge"] is True

    def test_no_skip_cc_plan_default(self):
        """skip-cc-plan 省略時は False"""
        result = parse_queue_line("Foo 1")
        assert result["skip_cc_plan"] is False

    def test_skip_test(self):
        """skip-test トークン → skip_test=True"""
        result = parse_queue_line("Foo 1 skip-test")
        assert result["skip_test"] is True

    def test_no_skip_test(self):
        """no-skip-test トークン → skip_test=False"""
        result = parse_queue_line("Foo 1 no-skip-test")
        assert result["skip_test"] is False

    def test_skip_test_default(self):
        """skip-test 省略時は False"""
        result = parse_queue_line("Foo 1")
        assert result["skip_test"] is False

    # --- デフォルトオプション注入テスト (Issue #133) ---

    def test_parse_queue_line_default_options_applied(self, monkeypatch):
        """DEFAULT_QUEUE_OPTIONS の値がオプション未指定行に注入される"""
        monkeypatch.setattr("task_queue.DEFAULT_QUEUE_OPTIONS", {"skip_cc_plan": True, "keep_ctx_intra": True})
        result = parse_queue_line("Foo 1")
        assert result["skip_cc_plan"] is True
        assert result["keep_ctx_intra"] is True

    def test_parse_queue_line_explicit_overrides_default(self, monkeypatch):
        """明示指定されたキーはデフォルトで上書きされない"""
        monkeypatch.setattr("task_queue.DEFAULT_QUEUE_OPTIONS", {"skip_cc_plan": True, "keep_ctx_intra": True})
        result = parse_queue_line("Foo 1 keep-ctx-batch")
        assert result["keep_ctx_batch"] is True       # 明示指定
        assert result["keep_ctx_intra"] is True       # デフォルト注入

    def test_parse_queue_line_keep_ctx_none_overrides_default(self, monkeypatch):
        """keep-ctx-none 指定時はデフォルトが適用されない"""
        monkeypatch.setattr("task_queue.DEFAULT_QUEUE_OPTIONS", {"skip_cc_plan": True, "keep_ctx_intra": True})
        result = parse_queue_line("Foo 1 keep-ctx-none")
        assert result["keep_ctx_batch"] is False
        assert result["keep_ctx_intra"] is False      # デフォルト True が適用されない

    def test_parse_queue_line_no_skip_cc_plan_overrides_default(self, monkeypatch):
        """no-skip-cc-plan 指定時はデフォルトの skip_cc_plan=True が適用されない"""
        monkeypatch.setattr("task_queue.DEFAULT_QUEUE_OPTIONS", {"skip_cc_plan": True})
        result = parse_queue_line("Foo 1 no-skip-cc-plan")
        assert result["skip_cc_plan"] is False

    def test_parse_queue_line_empty_defaults(self, monkeypatch):
        """DEFAULT_QUEUE_OPTIONS が空のときは従来通りの結果"""
        monkeypatch.setattr("task_queue.DEFAULT_QUEUE_OPTIONS", {})
        result = parse_queue_line("Foo 1")
        assert result["skip_cc_plan"] is False
        assert result["keep_ctx_intra"] is False

    def test_parse_queue_line_keep_ctx_none_token_valid(self, monkeypatch):
        """keep-ctx-none トークンが ValueError を投げない"""
        monkeypatch.setattr("task_queue.DEFAULT_QUEUE_OPTIONS", {})
        result = parse_queue_line("Foo 1 keep-ctx-none")
        assert result["keep_ctx_batch"] is False
        assert result["keep_ctx_intra"] is False

    def test_parse_queue_line_no_skip_cc_plan_token_valid(self, monkeypatch):
        """no-skip-cc-plan トークンが ValueError を投げない"""
        monkeypatch.setattr("task_queue.DEFAULT_QUEUE_OPTIONS", {})
        result = parse_queue_line("Foo 1 no-skip-cc-plan")
        assert result["skip_cc_plan"] is False

    # --- アンダーバー正規化テスト (Issue #165) ---

    def test_underscore_normalized_to_hyphen(self):
        """アンダーバーがハイフンに正規化される"""
        result = parse_queue_line("Foo 1 p2_fix")
        assert result["p2_fix"] is True

    def test_underscore_keep_ctx_intra(self):
        """keep_ctx_intra（アンダーバー）が認識される"""
        result = parse_queue_line("Foo 1 keep_ctx_intra")
        assert result["keep_ctx_intra"] is True

    def test_underscore_skip_cc_plan(self):
        """skip_cc_plan（アンダーバー）が認識される"""
        result = parse_queue_line("Foo 1 skip_cc_plan")
        assert result["skip_cc_plan"] is True

    def test_underscore_no_automerge(self):
        """no_automerge（アンダーバー）が認識される"""
        result = parse_queue_line("Foo 1 no_automerge")
        assert result["automerge"] is False

    def test_underscore_comment_value_preserved(self):
        """comment= の値部分はアンダーバーが保持される"""
        result = parse_queue_line("Foo 1 comment=some_note_here")
        assert result["comment"] == "some_note_here"

    def test_underscore_impl_model_preserved(self):
        """impl= のモデル名はアンダーバーが保持される"""
        result = parse_queue_line("Foo 1 impl=some_model")
        assert result["cc_impl_model"] == "some_model"

    # --- アンダーバー照合順序テスト (Issue #166) ---

    def test_underscore_review_mode_matched_without_conversion(self, monkeypatch):
        """REVIEW_MODES にアンダーバーで登録されているキーはそのままマッチする"""
        monkeypatch.setitem(task_queue.REVIEW_MODES, "lite3_woGoogle", {"members": [], "min_reviews": 1})
        result = parse_queue_line("Foo 1 lite3_woGoogle")
        assert result["mode"] == "lite3_woGoogle"

    def test_hyphen_review_mode_still_works(self, monkeypatch):
        """ハイフンで登録されているキーは変換なしでマッチする"""
        monkeypatch.setitem(task_queue.REVIEW_MODES, "lite3-woOpus", {"members": [], "min_reviews": 1})
        result = parse_queue_line("Foo 1 lite3-woOpus")
        assert result["mode"] == "lite3-woOpus"

    def test_underscore_review_mode_fallback_to_hyphen(self, monkeypatch):
        """アンダーバーで直接マッチしない場合はフォールバック変換で _ → - になる"""
        monkeypatch.setitem(task_queue.REVIEW_MODES, "lite3-woOpus", {"members": [], "min_reviews": 1})
        result = parse_queue_line("Foo 1 lite3_woOpus")
        assert result["mode"] == "lite3-woOpus"

    def test_underscore_known_option_unchanged(self):
        """既存のアンダーバー→ハイフン変換が壊れていないことの回帰テスト"""
        result = parse_queue_line("Foo 1 p2_fix")
        assert result["p2_fix"] is True

    # --- インラインコメントテスト (Issue #105) ---

    def test_inline_comment_space_hash_space(self):
        """末尾インラインコメント（スペース+#+スペース）: 正常パース"""
        result = parse_queue_line("baybay 1 lite # 骨格")
        assert result["project"] == "baybay"
        assert result["issues"] == "1"
        assert result["mode"] == "lite"

    def test_inline_comment_double_space_hash(self):
        """末尾インラインコメント（スペース2個+#）: 正常パース（空白の揺れに対応）"""
        result = parse_queue_line("baybay 1 lite  # 骨格")
        assert result["project"] == "baybay"
        assert result["issues"] == "1"
        assert result["mode"] == "lite"

    def test_inline_comment_tab_hash(self):
        """末尾インラインコメント（タブ+#）: 正常パース（タブ対応）"""
        result = parse_queue_line("baybay 1 lite\t# 骨格")
        assert result["project"] == "baybay"
        assert result["issues"] == "1"
        assert result["mode"] == "lite"

    def test_inline_comment_hash_only(self):
        """末尾が `#` のみ（スペース+#、コメント文なし）: 正常パース"""
        result = parse_queue_line("baybay 1 lite #")
        assert result["project"] == "baybay"
        assert result["issues"] == "1"
        assert result["mode"] == "lite"

    def test_inline_comment_with_multiple_options(self):
        """複数オプション後のインラインコメント: 正常パース"""
        result = parse_queue_line("baybay 1,2 automerge p2-fix # テスト")
        assert result["project"] == "baybay"
        assert result["issues"] == "1,2"
        assert result["automerge"] is True
        assert result["p2_fix"] is True

    def test_inline_comment_no_space_before_hash(self):
        """# 前後にスペースなし（非コメント）: Unknown token の ValueError"""
        with pytest.raises(ValueError, match="Unknown token"):
            parse_queue_line("baybay 1 lite#骨格")

    def test_comment_line_still_raises(self):
        """コメントのみ残る場合: 既存通り ValueError（行頭 # のスキップ）"""
        with pytest.raises(ValueError):
            parse_queue_line("# 全部コメント")

    def test_original_line_preserved(self):
        """`original_line` がインラインコメント除去前の元の行を保持する"""
        raw = "baybay 1 lite # 骨格"
        result = parse_queue_line(raw)
        assert result["original_line"] == raw

    def test_inline_comment_with_plan_option(self):
        """`plan=` オプションとインラインコメントの共存: 正常パース"""
        result = parse_queue_line("baybay 1 plan=sonnet # モデル指定")
        assert result["project"] == "baybay"
        assert result["issues"] == "1"
        assert result["cc_plan_model"] == "sonnet"


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


# ===========================================================================
# Issue #107: replace_entry テスト
# ===========================================================================

class TestReplaceEntry:
    """replace_entry() のテスト"""

    def test_replace_first(self, tmp_path):
        """index=0 で先頭行を置換"""
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("Foo 1\nBar 2\n")

        result = replace_entry(queue_file, 0, "Baz 3")
        assert result is not None
        assert result["project"] == "Baz"

        content = queue_file.read_text()
        assert "Baz 3\n" in content
        assert "Foo 1" not in content
        assert "Bar 2\n" in content

    def test_replace_last_keyword(self, tmp_path):
        """index='last' で末尾行を置換"""
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("Foo 1\nBar 2\nBaz 3\n")

        result = replace_entry(queue_file, "last", "Qux 4")
        assert result is not None
        assert result["project"] == "Qux"

        content = queue_file.read_text()
        assert "Qux 4\n" in content
        assert "Baz 3" not in content
        assert "Foo 1\n" in content
        assert "Bar 2\n" in content

    def test_replace_minus_one(self, tmp_path):
        """index='-1' で末尾行を置換"""
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("Foo 1\nBar 2\n")

        result = replace_entry(queue_file, "-1", "Baz 3")
        assert result is not None
        assert result["project"] == "Baz"

        content = queue_file.read_text()
        assert "Baz 3\n" in content
        assert "Bar 2" not in content

    def test_replace_out_of_range(self, tmp_path):
        """範囲外 → None"""
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("Foo 1\n")

        assert replace_entry(queue_file, 5, "Bar 2") is None

    def test_replace_empty_queue(self, tmp_path):
        """空キュー → None"""
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("")

        assert replace_entry(queue_file, 0, "Foo 1") is None

    def test_replace_skips_done_lines(self, tmp_path):
        """done行はインデックス対象外。index=0 は最初のactive行を置換"""
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("# done: Foo 1\nBar 2\nBaz 3\n")

        result = replace_entry(queue_file, 0, "Qux 4")
        assert result is not None
        assert result["project"] == "Qux"

        content = queue_file.read_text()
        assert "# done: Foo 1\n" in content
        assert "Qux 4\n" in content
        assert "Bar 2" not in content
        assert "Baz 3\n" in content

    def test_replace_file_not_exist(self, tmp_path):
        """ファイル不存在 → None"""
        queue_file = tmp_path / "nonexistent.txt"
        assert replace_entry(queue_file, 0, "Foo 1") is None

    def test_replace_invalid_new_line(self, tmp_path):
        """不正な新行 → ValueError（ファイルが存在してもバリデーションが先）"""
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("Foo 1\n")

        with pytest.raises(ValueError):
            replace_entry(queue_file, 0, "INVALID_NO_ISSUE")

    def test_replace_preserves_other_lines(self, tmp_path):
        """他のactive行・done行・コメント行が変化しないこと"""
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text(
            "# done: Foo 1\n"
            "# コメント行\n"
            "\n"
            "Bar 2\n"
            "Baz 3\n"
        )

        result = replace_entry(queue_file, 0, "Qux 4")
        assert result is not None
        assert result["project"] == "Qux"

        content = queue_file.read_text()
        assert "# done: Foo 1\n" in content
        assert "# コメント行\n" in content
        assert "Qux 4\n" in content
        assert "Bar 2" not in content
        assert "Baz 3\n" in content


# ===========================================================================
# Issue #107: cmd_qedit テスト
# ===========================================================================

class TestCmdQedit:
    """cmd_qedit() のテスト"""

    def _make_args(self, target, entry, queue=None):
        import argparse
        ns = argparse.Namespace(
            target=target,
            entry=entry,
            queue=queue,
        )
        return ns

    def test_cmd_qedit_success(self, tmp_path, monkeypatch, capsys):
        """正常置換: 成功メッセージ + キュー状態が stdout に出力される"""
        import config
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("Foo 1\nBar 2\n")
        monkeypatch.setattr(config, "QUEUE_FILE", queue_file)

        from gokrax import cmd_qedit
        args = self._make_args("0", ["Baz", "3"])
        cmd_qedit(args)

        out = capsys.readouterr().out
        assert "Replaced [0]: Baz 3" in out

    def test_cmd_qedit_last(self, tmp_path, monkeypatch, capsys):
        """target='last' で末尾行を置換"""
        import config
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("Foo 1\nBar 2\n")
        monkeypatch.setattr(config, "QUEUE_FILE", queue_file)

        from gokrax import cmd_qedit
        args = self._make_args("last", ["Baz", "3"])
        cmd_qedit(args)

        out = capsys.readouterr().out
        assert "Replaced [last]: Baz 3" in out
        content = queue_file.read_text()
        assert "Baz 3\n" in content
        assert "Bar 2" not in content

    def test_cmd_qedit_invalid_target(self, tmp_path, monkeypatch, capsys):
        """target が数値でも 'last'/'-1' でもない → stderr + sys.exit(1)"""
        import config
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("Foo 1\n")
        monkeypatch.setattr(config, "QUEUE_FILE", queue_file)

        from gokrax import cmd_qedit
        args = self._make_args("invalid", ["Bar", "2"])
        with pytest.raises(SystemExit) as exc:
            cmd_qedit(args)
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "invalid" in err

    def test_cmd_qedit_out_of_range(self, tmp_path, monkeypatch, capsys):
        """範囲外 → stderr + sys.exit(1)"""
        import config
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("Foo 1\n")
        monkeypatch.setattr(config, "QUEUE_FILE", queue_file)

        from gokrax import cmd_qedit
        args = self._make_args("99", ["Bar", "2"])
        with pytest.raises(SystemExit) as exc:
            cmd_qedit(args)
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "not found" in err or "empty" in err

    def test_cmd_qedit_invalid_entry(self, tmp_path, monkeypatch, capsys):
        """不正な新行 → stderr + sys.exit(1)"""
        import config
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("Foo 1\n")
        monkeypatch.setattr(config, "QUEUE_FILE", queue_file)

        from gokrax import cmd_qedit
        args = self._make_args("0", ["INVALID_NO_ISSUE"])
        with pytest.raises(SystemExit) as exc:
            cmd_qedit(args)
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "Error" in err


# ===========================================================================
# Issue #80: get_qstatus_text / _get_running_info テスト
# ===========================================================================

from gokrax import get_qstatus_text, _get_running_info


def _make_entry(idx=0, project="Foo", issues="1,2", mode="full", **kwargs):
    return {"index": idx, "project": project, "issues": issues, "mode": mode, **kwargs}


class TestGetQstatusText:

    def test_no_running_no_star_line(self):
        """running=None の場合 [*] 行が出ない（後方互換）。"""
        entries = [_make_entry(idx=0, project="Foo", issues="1")]
        text = get_qstatus_text(entries, running=None)
        assert "[*]" not in text
        assert "[0] Foo 1 full" in text

    def test_running_shows_star_line_at_top(self):
        """running がある場合 [*] 行が先頭に来る。"""
        entries = [_make_entry(idx=0, project="Bar", issues="2")]
        running = {"project": "Foo", "issues": "#1,#2", "state": "DESIGN_REVIEW", "review_mode": "full"}
        text = get_qstatus_text(entries, running=running)
        lines = text.splitlines()
        assert lines[0] == "[*] Foo #1,#2 DESIGN_REVIEW full"
        assert lines[1].startswith("[0]")

    def test_empty_entries_running_only(self):
        """entries 空でも running があれば [*] 行だけ返す。"""
        running = {"project": "Foo", "issues": "#1", "state": "IMPLEMENTATION", "review_mode": "standard"}
        text = get_qstatus_text([], running=running)
        assert text == "[*] Foo #1 IMPLEMENTATION standard"

    def test_running_issues_empty_string_omitted(self):
        """running['issues'] が空文字の場合、issues 部分が省略される。"""
        running = {"project": "Foo", "issues": "", "state": "IDLE_CHECK", "review_mode": "full"}
        text = get_qstatus_text([], running=running)
        assert "#" not in text
        assert "[*] Foo IDLE_CHECK full" == text

    def test_running_issues_none_omitted(self):
        """running['issues'] が None の場合、issues 部分が省略される。"""
        running = {"project": "Foo", "issues": None, "state": "IDLE_CHECK", "review_mode": "full"}
        text = get_qstatus_text([], running=running)
        assert "[*] Foo IDLE_CHECK full" == text

    def test_running_review_mode_empty_string_omitted(self):
        """running['review_mode'] が空文字の場合、review_mode 部分が省略される。"""
        running = {"project": "Foo", "issues": "#1", "state": "DESIGN_REVIEW", "review_mode": ""}
        text = get_qstatus_text([], running=running)
        assert text == "[*] Foo #1 DESIGN_REVIEW"

    def test_running_review_mode_none_omitted(self):
        """running['review_mode'] が None の場合、review_mode 部分が省略される。"""
        running = {"project": "Foo", "issues": "#1", "state": "DESIGN_REVIEW", "review_mode": None}
        text = get_qstatus_text([], running=running)
        assert text == "[*] Foo #1 DESIGN_REVIEW"

    def test_both_empty_returns_empty_string(self):
        """entries も running も空なら空文字列を返す。"""
        text = get_qstatus_text([], running=None)
        assert text == ""


class TestGetRunningInfo:

    def test_no_pipelines_returns_none(self, tmp_pipelines):
        """パイプラインなし → None。"""
        result = _get_running_info()
        assert result is None

    def test_idle_pipeline_returns_none(self, tmp_pipelines):
        """IDLE のパイプラインのみ → None。"""
        import json
        p = tmp_pipelines / "proj.json"
        p.write_text(json.dumps({"project": "Proj", "state": "IDLE", "batch": [], "review_mode": "full"}))
        result = _get_running_info()
        assert result is None

    def test_active_pipeline_returns_dict(self, tmp_pipelines):
        """state != IDLE のパイプライン1件 → dict を返す。"""
        import json
        p = tmp_pipelines / "proj.json"
        batch = [{"issue": 51, "title": "T"}, {"issue": 53, "title": "T2"}]
        p.write_text(json.dumps({
            "project": "EMCalibrator", "state": "DESIGN_REVIEW",
            "batch": batch, "review_mode": "full",
        }))
        result = _get_running_info()
        assert result is not None
        assert result["project"] == "EMCalibrator"
        assert result["issues"] == "#51,#53"
        assert result["state"] == "DESIGN_REVIEW"
        assert result["review_mode"] == "full"

    def test_multiple_active_returns_first_and_warns(self, tmp_pipelines, caplog):
        """複数 active → sorted 順で最初を返し、warning ログを出す。"""
        import json
        import logging
        (tmp_pipelines / "aaa.json").write_text(json.dumps(
            {"project": "AAA", "state": "IMPLEMENTATION", "batch": [], "review_mode": ""}
        ))
        (tmp_pipelines / "bbb.json").write_text(json.dumps(
            {"project": "BBB", "state": "CODE_REVIEW", "batch": [], "review_mode": ""}
        ))
        with caplog.at_level(logging.WARNING):
            result = _get_running_info()
        assert result is not None
        assert result["project"] == "AAA"  # sorted 順で aaa が先
        assert any("Multiple active pipelines" in r.message for r in caplog.records)


# ── TestSanitizeComment (Issue #88) ──────────────────────────────────────────

class TestSanitizeComment:
    """sanitize_comment() のテスト"""

    def test_strips_whitespace(self):
        """前後の空白を除去"""
        assert sanitize_comment("  テスト  ") == "テスト"

    def test_newline_to_space(self):
        """改行を半角スペースに正規化"""
        assert sanitize_comment("行1\n行2\r\n行3") == "行1 行2 行3"

    def test_at_mention_suppression(self):
        """@メンション抑止"""
        result = sanitize_comment("@everyone 注意")
        assert "@\u200beveryone 注意" == result

    def test_markdown_code_block_suppression(self):
        """Markdownコードブロック崩れ抑止"""
        result = sanitize_comment("コード```例```")
        assert "```" not in result
        assert "`\u200b``" in result

    def test_empty_returns_none(self):
        """空文字列 → None"""
        assert sanitize_comment("  ") is None
        assert sanitize_comment("") is None

    def test_mixed_sanitize(self):
        """複数サニタイズの組み合わせ"""
        result = sanitize_comment("@here```\n混合")
        assert "@\u200bhere" in result
        assert "`\u200b``" in result
        assert "\n" not in result


# ── TestParseQueueLineComment (Issue #88) ────────────────────────────────────

class TestParseQueueLineComment:
    """parse_queue_line() の comment= トークンテスト"""

    def test_no_comment_default_none(self):
        """comment= なし → comment が None"""
        result = parse_queue_line("Foo 1")
        assert result["comment"] is None

    def test_comment_basic(self):
        """comment=テスト → サニタイズ済み文字列"""
        result = parse_queue_line("Foo 1 comment=テスト")
        assert result["comment"] == "テスト"

    def test_comment_greedy(self):
        """comment= は残りの行全体を消費する（貪欲パース）"""
        result = parse_queue_line("Foo 1 lite comment=数学的正しさを 重視せよ")
        assert result["mode"] == "lite"
        assert result["comment"] == "数学的正しさを 重視せよ"

    def test_comment_empty_value(self):
        """comment= 以降が空 → None"""
        result = parse_queue_line("Foo 1 comment=")
        assert result["comment"] is None

    def test_comment_before_other_tokens_ignored(self):
        """comment= の前のトークンは正しくパースされる"""
        result = parse_queue_line("Foo 1 full automerge comment=注意")
        assert result["mode"] == "full"
        assert result["automerge"] is True
        assert result["comment"] == "注意"

    def test_comment_with_at_sanitized(self):
        """comment= 内の @メンションはサニタイズされる"""
        result = parse_queue_line("Foo 1 comment=@everyone テスト")
        assert result["comment"] == "@\u200beveryone テスト"


# ── TestCmdQadd (Issue #85) ──────────────────────────────────────────────────

class TestCmdQadd:
    """cmd_qadd() のテスト"""

    def _make_args(self, entry=None, file=None, from_stdin=False, queue=None):
        import argparse
        ns = argparse.Namespace(
            entry=entry or [],
            file=file,
            from_stdin=from_stdin,
            queue=queue,
        )
        return ns

    def test_positional_single_line(self, tmp_path, monkeypatch):
        """従来の位置引数1行指定 → 正常追加（後方互換）"""
        import config
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("")
        monkeypatch.setattr(config, "QUEUE_FILE", queue_file)

        from gokrax import cmd_qadd
        args = self._make_args(entry=["Foo", "1", "full", "automerge"])
        cmd_qadd(args)

        content = queue_file.read_text()
        assert "Foo 1 full automerge" in content

    def test_file_multiple_entries(self, tmp_path, monkeypatch):
        """--file で3行ファイル → 3エントリ追加"""
        import config
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("")
        monkeypatch.setattr(config, "QUEUE_FILE", queue_file)

        entries_file = tmp_path / "entries.txt"
        entries_file.write_text("Foo 1 full\nBar 2 lite\nBaz 3\n")

        from gokrax import cmd_qadd
        args = self._make_args(file=entries_file)
        cmd_qadd(args)

        content = queue_file.read_text()
        assert "Foo 1 full" in content
        assert "Bar 2 lite" in content
        assert "Baz 3" in content

    def test_stdin_multiple_entries(self, tmp_path, monkeypatch):
        """--stdin で2行入力 → 2エントリ追加"""
        import config, io
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("")
        monkeypatch.setattr(config, "QUEUE_FILE", queue_file)
        monkeypatch.setattr("sys.stdin", io.StringIO("Foo 1 full\nBar 2 lite\n"))

        from gokrax import cmd_qadd
        args = self._make_args(from_stdin=True)
        cmd_qadd(args)

        content = queue_file.read_text()
        assert "Foo 1 full" in content
        assert "Bar 2 lite" in content

    def test_file_validation_error_aborts_all(self, tmp_path, monkeypatch):
        """バリデーションエラー行があると全体中止・0件追加"""
        import config
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("")
        monkeypatch.setattr(config, "QUEUE_FILE", queue_file)

        entries_file = tmp_path / "entries.txt"
        entries_file.write_text("Foo 1 full\nINVALID_NO_ISSUE\nBar 2 lite\n")

        from gokrax import cmd_qadd
        args = self._make_args(file=entries_file)
        with pytest.raises(SystemExit):
            cmd_qadd(args)

        # ファイルが変更されていないこと
        assert queue_file.read_text() == ""

    def test_file_and_positional_mutually_exclusive(self, tmp_path, monkeypatch):
        """--file と位置引数の同時指定 → SystemExit"""
        import config
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("")
        monkeypatch.setattr(config, "QUEUE_FILE", queue_file)

        entries_file = tmp_path / "entries.txt"
        entries_file.write_text("Foo 1\n")

        from gokrax import cmd_qadd
        args = self._make_args(entry=["Foo", "1"], file=entries_file)
        with pytest.raises(SystemExit):
            cmd_qadd(args)

    def test_no_args_exits(self, tmp_path, monkeypatch):
        """引数なし → SystemExit"""
        import config
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("")
        monkeypatch.setattr(config, "QUEUE_FILE", queue_file)

        from gokrax import cmd_qadd
        args = self._make_args()
        with pytest.raises(SystemExit):
            cmd_qadd(args)

    def test_file_skips_empty_and_comment_lines(self, tmp_path, monkeypatch):
        """空行・コメント行はスキップ"""
        import config
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("")
        monkeypatch.setattr(config, "QUEUE_FILE", queue_file)

        entries_file = tmp_path / "entries.txt"
        entries_file.write_text("# comment\n\nFoo 1 full\n  \nBar 2 lite\n")

        from gokrax import cmd_qadd
        args = self._make_args(file=entries_file)
        cmd_qadd(args)

        content = queue_file.read_text()
        assert "Foo 1 full" in content
        assert "Bar 2 lite" in content
        assert "#" not in content


class TestCmdStartSkipCcPlan:
    """cmd_start の --skip-cc-plan フラグ・残留クリア・qrun dry-run のテスト"""

    @staticmethod
    def _write_pipeline(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        import json as _json
        path.write_text(_json.dumps(data))

    def test_skip_cc_plan_saved_to_pipeline(self, tmp_pipelines):
        """--skip-cc-plan が渡されたとき skip_cc_plan=True がパイプラインに保存されること"""
        import json as _json
        path = tmp_pipelines / "test-pj.json"
        self._write_pipeline(path, {
            "project": "test-pj", "gitlab": "atakalive/test-pj",
            "state": "IDLE", "enabled": False,
            "implementer": "kaneko", "batch": [], "history": [],
            "created_at": "2025-01-01T00:00:00+09:00",
            "updated_at": "2025-01-01T00:00:00+09:00",
        })
        import argparse
        from gokrax import cmd_start
        args = argparse.Namespace(
            project="test-pj",
            issue=[1],
            mode=None,
            keep_context=False,
            keep_ctx_batch=False,
            keep_ctx_intra=False,
            keep_ctx_all=False,
            p2_fix=False,
            comment=None,
            skip_cc_plan=True,
        )
        with patch("commands.dev.cmd_triage"), \
             patch("commands.dev.cmd_transition"), \
             patch("gokrax._start_loop"):
            cmd_start(args)

        data = _json.loads(path.read_text())
        assert data.get("skip_cc_plan") is True

    def test_stale_skip_cc_plan_cleared_without_flag(self, tmp_pipelines):
        """前回残留した skip_cc_plan=True が --skip-cc-plan なしの cmd_start でクリアされること"""
        import json as _json
        path = tmp_pipelines / "test-pj.json"
        self._write_pipeline(path, {
            "project": "test-pj", "gitlab": "atakalive/test-pj",
            "state": "IDLE", "enabled": False,
            "implementer": "kaneko", "batch": [], "history": [],
            "skip_cc_plan": True,  # 残留フラグ
            "created_at": "2025-01-01T00:00:00+09:00",
            "updated_at": "2025-01-01T00:00:00+09:00",
        })
        import argparse
        from gokrax import cmd_start
        args = argparse.Namespace(
            project="test-pj",
            issue=[1],
            mode=None,
            keep_context=False,
            keep_ctx_batch=False,
            keep_ctx_intra=False,
            keep_ctx_all=False,
            p2_fix=False,
            comment=None,
            skip_cc_plan=False,  # フラグなし
        )
        with patch("commands.dev.cmd_triage"), \
             patch("commands.dev.cmd_transition"), \
             patch("gokrax._start_loop"):
            cmd_start(args)

        data = _json.loads(path.read_text())
        assert "skip_cc_plan" not in data

    def test_qrun_dry_run_shows_skip_cc_plan(self, tmp_path, monkeypatch, capsys):
        """cmd_qrun --dry-run で skip-cc-plan が opts に表示されること"""
        import config
        import argparse
        queue_file = tmp_path / "queue.txt"
        queue_file.write_text("Foo 1,2 automerge skip-cc-plan\n")
        monkeypatch.setattr(config, "QUEUE_FILE", queue_file)

        from gokrax import cmd_qrun
        args = argparse.Namespace(dry_run=True, queue=None)
        cmd_qrun(args)

        captured = capsys.readouterr()
        assert "skip-cc-plan" in captured.out


class TestParseQueueLineDefaultModelOptions:
    """DEFAULT_QUEUE_OPTIONS の impl/plan モデル指定テスト"""

    def test_default_impl_equals_format(self, monkeypatch):
        """パターン A: "impl=opus": True が cc_impl_model に反映される"""
        monkeypatch.setattr("task_queue.DEFAULT_QUEUE_OPTIONS", {"impl=opus": True})
        result = parse_queue_line("Foo 1")
        assert result["cc_impl_model"] == "opus"

    def test_default_impl_str_format(self, monkeypatch):
        """パターン B: "impl": "opus" が cc_impl_model に反映される"""
        monkeypatch.setattr("task_queue.DEFAULT_QUEUE_OPTIONS", {"impl": "opus"})
        result = parse_queue_line("Foo 1")
        assert result["cc_impl_model"] == "opus"

    def test_default_plan_equals_format(self, monkeypatch):
        """パターン A: "plan=sonnet": True が cc_plan_model に反映される"""
        monkeypatch.setattr("task_queue.DEFAULT_QUEUE_OPTIONS", {"plan=sonnet": True})
        result = parse_queue_line("Foo 1")
        assert result["cc_plan_model"] == "sonnet"

    def test_default_plan_str_format(self, monkeypatch):
        """パターン B: "plan": "sonnet" が cc_plan_model に反映される"""
        monkeypatch.setattr("task_queue.DEFAULT_QUEUE_OPTIONS", {"plan": "sonnet"})
        result = parse_queue_line("Foo 1")
        assert result["cc_plan_model"] == "sonnet"

    def test_explicit_impl_overrides_default(self, monkeypatch):
        """キュー行で明示指定した impl= はデフォルトより優先される"""
        monkeypatch.setattr("task_queue.DEFAULT_QUEUE_OPTIONS", {"impl": "opus"})
        result = parse_queue_line("Foo 1 impl=haiku")
        assert result["cc_impl_model"] == "haiku"

    def test_explicit_plan_overrides_default(self, monkeypatch):
        """キュー行で明示指定した plan= はデフォルトより優先される"""
        monkeypatch.setattr("task_queue.DEFAULT_QUEUE_OPTIONS", {"plan=sonnet": True})
        result = parse_queue_line("Foo 1 plan=opus")
        assert result["cc_plan_model"] == "opus"

    def test_both_model_and_bool_defaults(self, monkeypatch):
        """モデル指定と bool 指定が混在する DEFAULT_QUEUE_OPTIONS"""
        monkeypatch.setattr("task_queue.DEFAULT_QUEUE_OPTIONS", {
            "impl": "opus",
            "skip_cc_plan": True,
            "keep_ctx_intra": True,
        })
        result = parse_queue_line("Foo 1")
        assert result["cc_impl_model"] == "opus"
        assert result["skip_cc_plan"] is True
        assert result["keep_ctx_intra"] is True

    def test_unknown_alias_ignored(self, monkeypatch):
        """_QUEUE_OPT_ALIASES に存在しない "key=value" 形式は無視される（エラーにならない）"""
        monkeypatch.setattr("task_queue.DEFAULT_QUEUE_OPTIONS", {"unknown=value": True})
        result = parse_queue_line("Foo 1")
        assert result["cc_impl_model"] is None
        assert result["cc_plan_model"] is None

    def test_pattern_a_false_disables(self, monkeypatch):
        """パターン A: "impl=opus": False は適用しない（無効化の意図）"""
        monkeypatch.setattr("task_queue.DEFAULT_QUEUE_OPTIONS", {"impl=opus": False})
        result = parse_queue_line("Foo 1")
        assert result["cc_impl_model"] is None

    def test_pattern_a_empty_value_skipped(self, monkeypatch):
        """パターン A: "impl=": True は空値のためスキップされる"""
        monkeypatch.setattr("task_queue.DEFAULT_QUEUE_OPTIONS", {"impl=": True})
        result = parse_queue_line("Foo 1")
        assert result["cc_impl_model"] is None


class TestCmdStartDefaultModelOptions:
    """cmd_start の DEFAULT_QUEUE_OPTIONS → pipeline.json cc_model 書き込みテスト"""

    @staticmethod
    def _write_pipeline(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        import json as _json
        path.write_text(_json.dumps(data))

    def _make_pipeline_data(self):
        return {
            "project": "test-pj", "gitlab": "atakalive/test-pj",
            "state": "IDLE", "enabled": False,
            "implementer": "kaneko", "batch": [], "history": [],
            "created_at": "2025-01-01T00:00:00+09:00",
            "updated_at": "2025-01-01T00:00:00+09:00",
        }

    def test_default_impl_equals_format_to_pipeline(self, tmp_pipelines, monkeypatch):
        """パターン A: "impl=opus": True → pipeline.json に cc_impl_model が書き込まれる"""
        import json as _json
        import config
        monkeypatch.setattr(config, "DEFAULT_QUEUE_OPTIONS", {"impl=opus": True})
        path = tmp_pipelines / "test-pj.json"
        self._write_pipeline(path, self._make_pipeline_data())
        import argparse
        from gokrax import cmd_start
        args = argparse.Namespace(
            project="test-pj", issue=[1], mode=None,
            keep_context=False, keep_ctx_batch=False, keep_ctx_intra=False,
            keep_ctx_all=False, p2_fix=False, comment=None,
            skip_cc_plan=False, skip_test=False, skip_assess=False,
        )
        with patch("commands.dev.cmd_triage"), \
             patch("commands.dev.cmd_transition"), \
             patch("gokrax._start_loop"):
            cmd_start(args)
        data = _json.loads(path.read_text())
        assert data.get("cc_impl_model") == "opus"

    def test_default_impl_str_format_to_pipeline(self, tmp_pipelines, monkeypatch):
        """パターン B: "impl": "opus" → pipeline.json に cc_impl_model が書き込まれる"""
        import json as _json
        import config
        monkeypatch.setattr(config, "DEFAULT_QUEUE_OPTIONS", {"impl": "opus"})
        path = tmp_pipelines / "test-pj.json"
        self._write_pipeline(path, self._make_pipeline_data())
        import argparse
        from gokrax import cmd_start
        args = argparse.Namespace(
            project="test-pj", issue=[1], mode=None,
            keep_context=False, keep_ctx_batch=False, keep_ctx_intra=False,
            keep_ctx_all=False, p2_fix=False, comment=None,
            skip_cc_plan=False, skip_test=False, skip_assess=False,
        )
        with patch("commands.dev.cmd_triage"), \
             patch("commands.dev.cmd_transition"), \
             patch("gokrax._start_loop"):
            cmd_start(args)
        data = _json.loads(path.read_text())
        assert data.get("cc_impl_model") == "opus"
