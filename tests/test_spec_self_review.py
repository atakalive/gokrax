"""tests/test_spec_self_review.py — Issue #77: SPEC_REVISE セルフレビュー結線テスト"""

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from spec_revise import (
    DEFAULT_SELF_REVIEW_CHECKLIST,
    build_self_review_prompt,
    parse_self_review_response,
)
from watchdog import _check_spec_revise
from pipeline_io import default_spec_config
from tests.conftest import write_pipeline

LOCAL_TZ = timezone(timedelta(hours=9))


def _now():
    return datetime(2026, 3, 1, 12, 0, 0, tzinfo=LOCAL_TZ)


def _past(sec=7200):
    """タイムアウトを超えた過去の時刻（デフォルト 2h 前）。"""
    return (_now() - timedelta(seconds=sec)).isoformat()


def _make_spec_config(**overrides):
    cfg = default_spec_config()
    cfg.update(overrides)
    return cfg


def _make_pipeline(state="SPEC_REVISE", spec_config=None, **kwargs):
    data = {
        "project": "test-pj",
        "gitlab": "testns/test-pj",
        "state": state,
        "spec_mode": True,
        "spec_config": spec_config if spec_config is not None else {},
        "enabled": True,
        "implementer": "implementer1",
        "review_mode": "full",
        "batch": [],
        "history": [],
        "created_at": "2025-01-01T00:00:00+09:00",
        "updated_at": "2025-01-01T00:00:00+09:00",
    }
    data.update(kwargs)
    return data


def _args(**kwargs):
    return argparse.Namespace(**kwargs)


def _default_yaml_all_yes():
    """デフォルトチェックリスト全 Yes の YAML ブロック。"""
    return """\
```yaml
checklist:
  - id: "reflected_items_match"
    result: "Yes"
    evidence: "§3.2 に反映確認"
  - id: "no_new_contradictions"
    result: "Yes"
    evidence: "矛盾なし"
  - id: "pseudocode_consistency"
    result: "Yes"
    evidence: "引数一致"
  - id: "deferred_reasons_valid"
    result: "Yes"
    evidence: "全 deferred に理由あり"
```
"""


def _default_yaml_one_no():
    """デフォルトチェックリスト 1件 No の YAML ブロック。"""
    return """\
```yaml
checklist:
  - id: "reflected_items_match"
    result: "Yes"
    evidence: "OK"
  - id: "no_new_contradictions"
    result: "No"
    evidence: "§4 と §7 の定義が矛盾"
  - id: "pseudocode_consistency"
    result: "Yes"
    evidence: "OK"
  - id: "deferred_reasons_valid"
    result: "Yes"
    evidence: "OK"
```
"""


# ---------------------------------------------------------------------------
# 1. parse_self_review_response — 基本パターン
# ---------------------------------------------------------------------------

class TestParseSelfReviewResponse:

    def test_parse_self_review_all_yes(self):
        """全項目 Yes → verdict: clean"""
        result = parse_self_review_response(_default_yaml_all_yes())
        assert result["verdict"] == "clean"
        assert result["items"] == []

    def test_parse_self_review_some_no(self):
        """1項目 No → verdict: issues_found, items にその項目"""
        result = parse_self_review_response(_default_yaml_one_no())
        assert result["verdict"] == "issues_found"
        assert len(result["items"]) == 1
        assert result["items"][0]["id"] == "no_new_contradictions"
        assert result["items"][0]["result"] == "No"
        assert "§4" in result["items"][0]["evidence"]

    def test_parse_self_review_parse_failed(self):
        """不正 YAML → verdict: parse_failed"""
        result = parse_self_review_response("```yaml\n{not: [valid: yaml\n```")
        assert result["verdict"] == "parse_failed"
        assert result["items"] == []

    def test_parse_self_review_no_yaml_block(self):
        """YAML ブロックなし → parse_failed（テキストフォールバック削除の確認）"""
        # 旧実装は "status: clean" テキストを拾っていたが、新実装は拾わない
        result = parse_self_review_response("status: clean\nAll checks passed.")
        assert result["verdict"] == "parse_failed"

    def test_parse_self_review_case_insensitive(self):
        """result が "YES"/"yes"/"Yes" いずれも Yes 判定"""
        yaml_text = """\
```yaml
checklist:
  - id: "reflected_items_match"
    result: "YES"
    evidence: ""
  - id: "no_new_contradictions"
    result: "yes"
    evidence: ""
  - id: "pseudocode_consistency"
    result: "Yes"
    evidence: ""
  - id: "deferred_reasons_valid"
    result: "YES"
    evidence: ""
```
"""
        result = parse_self_review_response(yaml_text)
        assert result["verdict"] == "clean"

    def test_parse_self_review_id_mismatch(self):
        """期待 ID と応答 ID が不一致 → parse_failed"""
        yaml_text = """\
```yaml
checklist:
  - id: "reflected_items_match"
    result: "Yes"
    evidence: ""
  - id: "wrong_id"
    result: "Yes"
    evidence: ""
  - id: "pseudocode_consistency"
    result: "Yes"
    evidence: ""
  - id: "deferred_reasons_valid"
    result: "Yes"
    evidence: ""
```
"""
        result = parse_self_review_response(yaml_text)
        assert result["verdict"] == "parse_failed"

    def test_parse_self_review_duplicate_id(self):
        """同一 ID が重複 → parse_failed"""
        yaml_text = """\
```yaml
checklist:
  - id: "reflected_items_match"
    result: "Yes"
    evidence: ""
  - id: "reflected_items_match"
    result: "Yes"
    evidence: ""
  - id: "pseudocode_consistency"
    result: "Yes"
    evidence: ""
  - id: "deferred_reasons_valid"
    result: "Yes"
    evidence: ""
```
"""
        result = parse_self_review_response(yaml_text)
        assert result["verdict"] == "parse_failed"

    def test_parse_self_review_unknown_id(self):
        """未知 ID が混入（過不足あり）→ parse_failed"""
        yaml_text = """\
```yaml
checklist:
  - id: "reflected_items_match"
    result: "Yes"
    evidence: ""
  - id: "no_new_contradictions"
    result: "Yes"
    evidence: ""
  - id: "pseudocode_consistency"
    result: "Yes"
    evidence: ""
  - id: "deferred_reasons_valid"
    result: "Yes"
    evidence: ""
  - id: "extra_unknown_id"
    result: "Yes"
    evidence: ""
```
"""
        result = parse_self_review_response(yaml_text)
        assert result["verdict"] == "parse_failed"

    def test_parse_self_review_evidence_type(self):
        """evidence が非 str（数値）→ parse_failed"""
        yaml_text = """\
```yaml
checklist:
  - id: "reflected_items_match"
    result: "Yes"
    evidence: 123
  - id: "no_new_contradictions"
    result: "Yes"
    evidence: ""
  - id: "pseudocode_consistency"
    result: "Yes"
    evidence: ""
  - id: "deferred_reasons_valid"
    result: "Yes"
    evidence: ""
```
"""
        result = parse_self_review_response(yaml_text)
        assert result["verdict"] == "parse_failed"

    def test_parse_self_review_expected_ids_custom(self):
        """expected_ids が指定された場合はそれを使用する。"""
        yaml_text = """\
```yaml
checklist:
  - id: "custom_check"
    result: "Yes"
    evidence: "OK"
```
"""
        result = parse_self_review_response(yaml_text, expected_ids=["custom_check"])
        assert result["verdict"] == "clean"


# ---------------------------------------------------------------------------
# 2. build_self_review_prompt
# ---------------------------------------------------------------------------

class TestBuildSelfReviewPrompt:

    def test_build_self_review_prompt_default_checklist(self):
        """デフォルトチェックリスト 4 項目がプロンプトに含まれる。"""
        sc = {"spec_path": "docs/spec.md", "current_rev": "2", "last_commit": "abc1234"}
        prompt = build_self_review_prompt(sc, {"project": "gokrax"})
        for item in DEFAULT_SELF_REVIEW_CHECKLIST:
            assert item["id"] in prompt
            assert item["question"] in prompt
        assert "gokrax" in prompt
        assert "self-review-submit" in prompt

    def test_build_self_review_prompt_custom_checklist(self):
        """spec_config.self_review_checklist でカスタムチェックリストが使われる。"""
        custom = [
            {"id": "my_check", "question": "カスタムチェック"},
        ]
        sc = {
            "spec_path": "docs/spec.md",
            "current_rev": "1",
            "last_commit": "abc1234",
            "self_review_checklist": custom,
        }
        prompt = build_self_review_prompt(sc, {"project": "test-pj"})
        assert "my_check" in prompt
        assert "カスタムチェック" in prompt
        # デフォルト項目は含まれない
        assert "reflected_items_match" not in prompt

    def test_build_self_review_prompt_checklist_arg(self):
        """checklist 引数で直接渡した場合も使われる。"""
        custom = [{"id": "arg_check", "question": "引数チェック"}]
        sc = {"spec_path": "docs/spec.md", "current_rev": "1", "last_commit": "x" * 7}
        prompt = build_self_review_prompt(sc, {"project": "pj"}, checklist=custom)
        assert "arg_check" in prompt
        assert "引数チェック" in prompt


# ---------------------------------------------------------------------------
# 3. _check_spec_revise — watchdog 統合テスト
# ---------------------------------------------------------------------------

class TestCheckSpecReviseSelfReview:

    def test_check_spec_revise_self_review_clean(self):
        """self_review_response が clean → SPEC_REVIEW 遷移。"""
        sc = _make_spec_config(
            spec_implementer="implementer1",
            review_requests={
                "reviewer1": {"status": "pending", "sent_at": None, "timeout_at": None,
                           "last_nudge_at": None, "response": None},
            },
            _revise_sent="2026-03-01T10:00:00+09:00",
            _self_review_sent="2026-03-01T11:00:00+09:00",
            _self_review_response=_default_yaml_all_yes(),
            _self_review_pass=0,
            _self_review_pending_updates={
                "last_commit": "abc1234x",
                "current_rev": "2",
                "rev_index": 2,
                "last_changes": {"added_lines": 10, "removed_lines": 5},
                "revise_count": 1,
                "review_history": [],
                "current_reviews": {"entries": {}},
                "review_requests_patch": {},
                "_revise_retry_at": None,
            },
            _self_review_expected_ids=[c["id"] for c in DEFAULT_SELF_REVIEW_CHECKLIST],
        )
        data = _make_pipeline(spec_config=sc)
        action = _check_spec_revise(sc, _now(), data)
        assert action.next_state == "SPEC_REVIEW"
        pu = action.pipeline_updates
        assert pu.get("_self_review_sent") is None
        assert pu.get("_self_review_response") is None
        assert pu.get("_self_review_pass") == 0
        assert pu.get("_self_review_pending_updates") is None
        assert pu.get("_revise_sent") is None

    def test_check_spec_revise_self_review_issues(self):
        """self_review issues_found → SPEC_REVISE のまま差し戻し。_self_review_pass は 0 のまま。"""
        sc = _make_spec_config(
            spec_implementer="implementer1",
            _revise_sent="2026-03-01T10:00:00+09:00",
            _self_review_sent="2026-03-01T11:00:00+09:00",
            _self_review_response=_default_yaml_one_no(),
            _self_review_pass=0,
            _self_review_pending_updates={"current_rev": "2"},
            _self_review_expected_ids=[c["id"] for c in DEFAULT_SELF_REVIEW_CHECKLIST],
        )
        data = _make_pipeline(spec_config=sc)
        action = _check_spec_revise(sc, _now(), data)
        assert action.next_state is None  # SPEC_REVISE のまま
        pu = action.pipeline_updates
        # issues_found では _self_review_pass はインクリメントしない
        assert pu.get("_self_review_pass") == 0
        # self_review フィールドはクリア
        assert pu.get("_self_review_sent") is None
        assert pu.get("_self_review_response") is None
        assert pu.get("_self_review_pending_updates") is None
        # implementer に send_to が設定される
        assert action.send_to is not None
        assert "implementer1" in action.send_to

    def test_check_spec_revise_self_review_issues_no_double_send(self):
        """issues_found 差し戻し後、_revise_sent が更新されており (E) の初回送信が発火しない。"""
        sc = _make_spec_config(
            spec_implementer="implementer1",
            _revise_sent="2026-03-01T10:00:00+09:00",
            _self_review_sent="2026-03-01T11:00:00+09:00",
            _self_review_response=_default_yaml_one_no(),
            _self_review_pass=0,
            _self_review_expected_ids=[c["id"] for c in DEFAULT_SELF_REVIEW_CHECKLIST],
        )
        data = _make_pipeline(spec_config=sc)
        action = _check_spec_revise(sc, _now(), data)
        pu = action.pipeline_updates
        # _revise_sent が now に更新されている → 次の tick で (E) が発火しない
        assert pu.get("_revise_sent") == _now().isoformat()

    def test_check_spec_revise_self_review_issues_timeout_reset(self):
        """issues_found 後の _revise_sent が now に更新され、(D) のタイムアウト基準がリセットされる。"""
        sc = _make_spec_config(
            spec_implementer="implementer1",
            _revise_sent="2026-02-28T10:00:00+09:00",  # 古い値
            _self_review_sent="2026-03-01T11:00:00+09:00",
            _self_review_response=_default_yaml_one_no(),
            _self_review_pass=0,
            _self_review_expected_ids=[c["id"] for c in DEFAULT_SELF_REVIEW_CHECKLIST],
        )
        data = _make_pipeline(spec_config=sc)
        action = _check_spec_revise(sc, _now(), data)
        pu = action.pipeline_updates
        # 古い _revise_sent は now に更新されていること（タイムアウト基準リセット）
        assert pu.get("_revise_sent") == _now().isoformat()

    def test_check_spec_revise_self_review_parse_fail_retry(self):
        """self_review parse_failed → リトライ（_self_review_pass +1）。"""
        sc = _make_spec_config(
            spec_implementer="implementer1",
            self_review_agent="reviewer1",
            _revise_sent="2026-03-01T10:00:00+09:00",
            _self_review_sent="2026-03-01T11:00:00+09:00",
            _self_review_response="```yaml\nnot valid\n```",
            _self_review_pass=0,
            _self_review_expected_ids=[c["id"] for c in DEFAULT_SELF_REVIEW_CHECKLIST],
        )
        data = _make_pipeline(spec_config=sc)
        action = _check_spec_revise(sc, _now(), data)
        assert action.next_state is None
        assert action.pipeline_updates.get("_self_review_pass") == 1
        assert action.send_to is not None  # 再送信

    def test_check_spec_revise_self_review_parse_fail_max(self):
        """parse_failed が SPEC_REVISE_SELF_REVIEW_PASSES 回到達 → SPEC_PAUSED。"""
        from config import SPEC_REVISE_SELF_REVIEW_PASSES
        sc = _make_spec_config(
            spec_implementer="implementer1",
            self_review_agent="reviewer1",
            _revise_sent="2026-03-01T10:00:00+09:00",
            _self_review_sent="2026-03-01T11:00:00+09:00",
            _self_review_response="```yaml\nnot valid\n```",
            _self_review_pass=SPEC_REVISE_SELF_REVIEW_PASSES - 1,
            _self_review_expected_ids=[c["id"] for c in DEFAULT_SELF_REVIEW_CHECKLIST],
        )
        data = _make_pipeline(spec_config=sc)
        action = _check_spec_revise(sc, _now(), data)
        assert action.next_state == "SPEC_PAUSED"
        assert action.pipeline_updates.get("paused_from") == "SPEC_REVISE"

    def test_check_spec_revise_self_review_timeout(self):
        """self_review タイムアウト → リトライ（_self_review_pass +1）。"""
        from config import SPEC_BLOCK_TIMERS
        sc = _make_spec_config(
            spec_implementer="implementer1",
            self_review_agent="reviewer1",
            _revise_sent="2026-03-01T10:00:00+09:00",
            _self_review_sent=_past(SPEC_BLOCK_TIMERS["SPEC_REVIEW"] + 60),
            _self_review_response=None,
            _self_review_pass=0,
            _self_review_expected_ids=[c["id"] for c in DEFAULT_SELF_REVIEW_CHECKLIST],
        )
        data = _make_pipeline(spec_config=sc)
        action = _check_spec_revise(sc, _now(), data)
        assert action.next_state is None
        assert action.pipeline_updates.get("_self_review_pass") == 1
        assert action.send_to is not None  # 再送信

    def test_check_spec_revise_self_review_timeout_max(self):
        """self_review タイムアウトが最大回数 → SPEC_PAUSED。"""
        from config import SPEC_BLOCK_TIMERS, SPEC_REVISE_SELF_REVIEW_PASSES
        sc = _make_spec_config(
            spec_implementer="implementer1",
            self_review_agent="reviewer1",
            _revise_sent="2026-03-01T10:00:00+09:00",
            _self_review_sent=_past(SPEC_BLOCK_TIMERS["SPEC_REVIEW"] + 60),
            _self_review_response=None,
            _self_review_pass=SPEC_REVISE_SELF_REVIEW_PASSES - 1,
            _self_review_expected_ids=[c["id"] for c in DEFAULT_SELF_REVIEW_CHECKLIST],
        )
        data = _make_pipeline(spec_config=sc)
        action = _check_spec_revise(sc, _now(), data)
        assert action.next_state == "SPEC_PAUSED"
        assert action.pipeline_updates.get("paused_from") == "SPEC_REVISE"

    def test_check_spec_revise_self_review_init_clears_response(self):
        """(C) 改訂報告パース成功後、_self_review_response が None にクリアされる。"""
        revise_yaml = """\
```yaml
status: done
new_rev: "2"
commit: abc1234def
changes:
  added_lines: 10
  removed_lines: 5
```
"""
        sc = _make_spec_config(
            spec_implementer="implementer1",
            self_review_agent="reviewer1",
            spec_path="/repo/docs/test-spec.md",
            current_rev="1",
            review_requests={
                "reviewer1": {"status": "pending", "sent_at": None, "timeout_at": None,
                           "last_nudge_at": None, "response": None},
            },
            _revise_sent="2026-03-01T10:00:00+09:00",
            _revise_response=revise_yaml,
        )
        data = _make_pipeline(spec_config=sc)
        action = _check_spec_revise(sc, _now(), data)
        # self_review フェーズ開始のはず
        assert action.next_state is None
        assert action.pipeline_updates.get("_self_review_sent") is not None
        # _self_review_response が明示的に None にセットされていること（残骸クリア）
        assert "_self_review_response" in action.pipeline_updates
        assert action.pipeline_updates["_self_review_response"] is None

    def test_check_spec_revise_self_review_init_preserves_revise_sent(self):
        """(C) で _revise_sent がクリアされないこと。"""
        revise_yaml = """\
```yaml
status: done
new_rev: "2"
commit: abc1234def
changes:
  added_lines: 10
  removed_lines: 5
```
"""
        sc = _make_spec_config(
            spec_implementer="implementer1",
            self_review_agent="reviewer1",
            spec_path="/repo/docs/test-spec.md",
            current_rev="1",
            review_requests={
                "reviewer1": {"status": "pending", "sent_at": None, "timeout_at": None,
                           "last_nudge_at": None, "response": None},
            },
            _revise_sent="2026-03-01T10:00:00+09:00",
            _revise_response=revise_yaml,
        )
        data = _make_pipeline(spec_config=sc)
        action = _check_spec_revise(sc, _now(), data)
        pu = action.pipeline_updates
        # _revise_sent は pipeline_updates に含まれない（クリアしない）か、
        # 含まれていても None ではないこと
        if "_revise_sent" in pu:
            assert pu["_revise_sent"] is not None

    def test_check_spec_revise_self_review_custom_checklist_ids(self):
        """カスタムチェックリスト使用時に _self_review_expected_ids が正しく保存される。"""
        custom = [{"id": "custom_a", "question": "Aチェック"}]
        revise_yaml = """\
```yaml
status: done
new_rev: "2"
commit: abc1234def
changes:
  added_lines: 10
  removed_lines: 5
```
"""
        sc = _make_spec_config(
            spec_implementer="implementer1",
            self_review_agent="reviewer1",
            spec_path="/repo/docs/test-spec.md",
            current_rev="1",
            self_review_checklist=custom,
            review_requests={
                "reviewer1": {"status": "pending", "sent_at": None, "timeout_at": None,
                           "last_nudge_at": None, "response": None},
            },
            _revise_sent="2026-03-01T10:00:00+09:00",
            _revise_response=revise_yaml,
        )
        data = _make_pipeline(spec_config=sc)
        action = _check_spec_revise(sc, _now(), data)
        pu = action.pipeline_updates
        assert pu.get("_self_review_expected_ids") == ["custom_a"]


# ---------------------------------------------------------------------------
# 4. CLI: cmd_spec_self_review_submit
# ---------------------------------------------------------------------------

class TestCmdSpecSelfReviewSubmit:

    def test_cmd_spec_self_review_submit(self, tmp_pipelines, tmp_path):
        """通常投入: _self_review_response に格納される。"""
        sc = _make_spec_config(
            _self_review_sent="2026-03-01T11:00:00+09:00",
            _self_review_response=None,
        )
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="SPEC_REVISE", spec_config=sc))

        f = tmp_path / "self_review.yaml"
        f.write_text(_default_yaml_all_yes(), encoding="utf-8")

        from gokrax import cmd_spec_self_review_submit
        cmd_spec_self_review_submit(_args(project="test-pj", file=str(f)))

        data = json.loads(path.read_text())
        assert data["spec_config"]["_self_review_response"] is not None

    def test_cmd_spec_self_review_submit_not_requested(self, tmp_pipelines, tmp_path):
        """_self_review_sent がないとき → エラー。"""
        sc = _make_spec_config()
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="SPEC_REVISE", spec_config=sc))

        f = tmp_path / "self_review.yaml"
        f.write_text(_default_yaml_all_yes(), encoding="utf-8")

        from gokrax import cmd_spec_self_review_submit
        with pytest.raises(SystemExit, match="Self-review not requested"):
            cmd_spec_self_review_submit(_args(project="test-pj", file=str(f)))

    def test_cmd_spec_self_review_submit_already_submitted(self, tmp_pipelines, tmp_path, capsys):
        """_self_review_response が既にある場合 → スキップ（上書き禁止）。"""
        sc = _make_spec_config(
            _self_review_sent="2026-03-01T11:00:00+09:00",
            _self_review_response="already stored",
        )
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="SPEC_REVISE", spec_config=sc))

        f = tmp_path / "self_review.yaml"
        f.write_text(_default_yaml_all_yes(), encoding="utf-8")

        from gokrax import cmd_spec_self_review_submit
        cmd_spec_self_review_submit(_args(project="test-pj", file=str(f)))

        out = capsys.readouterr().out
        assert "skipping" in out
        # 元の値が保持されていること
        data = json.loads(path.read_text())
        assert data["spec_config"]["_self_review_response"] == "already stored"

    def test_cmd_spec_self_review_submit_fence_complement(self, tmp_pipelines, tmp_path):
        """YAML ブロックなしでも CLI がフェンス補完を試みる（parse 失敗でも格納する）。"""
        sc = _make_spec_config(
            _self_review_sent="2026-03-01T11:00:00+09:00",
            _self_review_response=None,
        )
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(state="SPEC_REVISE", spec_config=sc))

        # フェンスなし（raw YAML）で渡す
        raw_yaml = """\
checklist:
  - id: "reflected_items_match"
    result: "Yes"
    evidence: "OK"
  - id: "no_new_contradictions"
    result: "Yes"
    evidence: "OK"
  - id: "pseudocode_consistency"
    result: "Yes"
    evidence: "OK"
  - id: "deferred_reasons_valid"
    result: "Yes"
    evidence: "OK"
"""
        f = tmp_path / "self_review.yaml"
        f.write_text(raw_yaml, encoding="utf-8")

        from gokrax import cmd_spec_self_review_submit
        cmd_spec_self_review_submit(_args(project="test-pj", file=str(f)))

        data = json.loads(path.read_text())
        stored = data["spec_config"]["_self_review_response"]
        # フェンスで補完されて格納されていること
        assert stored.startswith("```yaml\n")


# ---------------------------------------------------------------------------
# P0 修正テスト (#77 code revise)
# ---------------------------------------------------------------------------

class TestP0Fixes:
    """#77 コードレビュー P0 修正の検証。"""

    def test_parse_self_review_unhashable_id(self):
        """Leibniz P0: unhashable id (list/dict) で parse_failed になること。"""
        yaml_text = """\
```yaml
checklist:
  - id: ["reflected_items_match"]
    result: "Yes"
    evidence: "OK"
  - id: "no_new_contradictions"
    result: "Yes"
    evidence: "OK"
  - id: "pseudocode_consistency"
    result: "Yes"
    evidence: "OK"
  - id: "deferred_reasons_valid"
    result: "Yes"
    evidence: "OK"
```
"""
        result = parse_self_review_response(yaml_text)
        assert result["verdict"] == "parse_failed"

    def test_parse_self_review_dict_id(self):
        """Leibniz P0: dict id で parse_failed になること。"""
        yaml_text = """\
```yaml
checklist:
  - id: {key: "val"}
    result: "Yes"
    evidence: "OK"
  - id: "no_new_contradictions"
    result: "Yes"
    evidence: "OK"
  - id: "pseudocode_consistency"
    result: "Yes"
    evidence: "OK"
  - id: "deferred_reasons_valid"
    result: "Yes"
    evidence: "OK"
```
"""
        result = parse_self_review_response(yaml_text)
        assert result["verdict"] == "parse_failed"

    def test_parse_self_review_int_id(self):
        """非文字列 id (int) で parse_failed になること。"""
        yaml_text = """\
```yaml
checklist:
  - id: 42
    result: "Yes"
    evidence: "OK"
  - id: "no_new_contradictions"
    result: "Yes"
    evidence: "OK"
  - id: "pseudocode_consistency"
    result: "Yes"
    evidence: "OK"
  - id: "deferred_reasons_valid"
    result: "Yes"
    evidence: "OK"
```
"""
        result = parse_self_review_response(yaml_text)
        assert result["verdict"] == "parse_failed"

    def test_issues_found_clears_revise_retry_at(self):
        """Euler P0-1: issues_found で _revise_retry_at がクリアされること。"""
        sc = _make_spec_config(
            _self_review_response=_default_yaml_one_no(),
            _self_review_sent=_now().isoformat(),
            _self_review_pass=0,
            _self_review_pending_updates={"current_rev": "2"},
            _self_review_expected_ids=None,
            _revise_retry_at="2026-02-28T10:00:00+09:00",
            _revise_sent="2026-02-28T10:00:00+09:00",
            spec_implementer="implementer2",
        )
        result = _check_spec_revise(sc, _now(), _make_pipeline(spec_config=sc))
        assert result.next_state is None  # SPEC_REVISE のまま
        assert result.pipeline_updates.get("_revise_retry_at") is None
        assert "_revise_retry_at" in result.pipeline_updates  # 明示的に None が入っている

    def test_clean_with_missing_pending_updates_pauses(self):
        """Euler Major: _self_review_pending_updates 欠落で SPEC_PAUSED。"""
        sc = _make_spec_config(
            _self_review_response=_default_yaml_all_yes(),
            _self_review_sent=_now().isoformat(),
            _self_review_pass=0,
            _self_review_pending_updates=None,  # 欠落
            _self_review_expected_ids=None,
        )
        result = _check_spec_revise(sc, _now(), _make_pipeline(spec_config=sc))
        assert result.next_state == "SPEC_PAUSED"
        assert "pending_updates" in (result.discord_notify or "").lower() or "欠落" in (result.discord_notify or "")

    def test_clean_with_non_dict_pending_updates_pauses(self):
        """Euler Major: _self_review_pending_updates が非dict で SPEC_PAUSED。"""
        sc = _make_spec_config(
            _self_review_response=_default_yaml_all_yes(),
            _self_review_sent=_now().isoformat(),
            _self_review_pass=0,
            _self_review_pending_updates="corrupted string",
            _self_review_expected_ids=None,
        )
        result = _check_spec_revise(sc, _now(), _make_pipeline(spec_config=sc))
        assert result.next_state == "SPEC_PAUSED"

    def test_self_review_sent_bad_iso_retries(self):
        """Euler Minor-B: _self_review_sent の日付パース失敗で永久待ちにならず、リトライする。"""
        sc = _make_spec_config(
            _self_review_sent="not-a-date",
            _self_review_response=None,
            _self_review_pass=0,
            _self_review_pending_updates={"current_rev": "2"},
            _self_review_expected_ids=None,
            spec_implementer="implementer2",
        )
        result = _check_spec_revise(sc, _now(), _make_pipeline(spec_config=sc))
        # リトライ（next_state=None, send_to あり, pass インクリメント）
        assert result.next_state is None
        assert result.send_to is not None
        assert result.pipeline_updates.get("_self_review_pass") == 1

    def test_self_review_sent_bad_iso_max_pauses(self):
        """Euler Minor-B: パース失敗 + max pass で SPEC_PAUSED。"""
        from config import SPEC_REVISE_SELF_REVIEW_PASSES
        sc = _make_spec_config(
            _self_review_sent="not-a-date",
            _self_review_response=None,
            _self_review_pass=SPEC_REVISE_SELF_REVIEW_PASSES - 1,
            _self_review_pending_updates={"current_rev": "2"},
            _self_review_expected_ids=None,
            spec_implementer="implementer2",
        )
        result = _check_spec_revise(sc, _now(), _make_pipeline(spec_config=sc))
        assert result.next_state == "SPEC_PAUSED"


# ---------------------------------------------------------------------------
# #327 verification: B2 (self_review send) BUSY/FAIL paths
# Spec Test Plan units 7, 8, 9, 10, 10b, 10c, 10d (dijkstra P2-2)
# ---------------------------------------------------------------------------

class TestB2BusyFail:
    """B2 (self_review send pending) の BUSY / FAIL / escalation 検証。"""

    def _b2_spec_config(self, **overrides):
        """B2 経路を発火させる最小 spec_config: pending_updates あり, _self_review_sent=None。"""
        cfg = _make_spec_config(
            spec_implementer="implementer1",
            self_review_agent="implementer1",
            spec_path="docs/spec.md",
            current_rev="1",
            _self_review_sent=None,
            _self_review_response=None,
            _self_review_pass=0,
            _self_review_pending_updates={
                "current_rev": "2",
                "last_commit": "abc1234",
            },
            _self_review_expected_ids=[c["id"] for c in DEFAULT_SELF_REVIEW_CHECKLIST],
        )
        cfg.update(overrides)
        return cfg

    def _write_pj(self, tmp_pipelines, sc):
        pj_path = tmp_pipelines / "test-pj.json"
        write_pipeline(pj_path, _make_pipeline(state="SPEC_REVISE", spec_config=sc))
        return pj_path

    def test_b2_busy_rolls_back_retries_and_sets_busy_since(self, tmp_pipelines):
        """7: BUSY 受信 → _self_review_pass 巻き戻し, _self_review_sent ロールバック, busy_since 初期化。"""
        from engine.fsm_spec import _check_spec_revise, _apply_spec_action
        from engine.backend_types import SendResult
        from unittest.mock import patch

        sc = self._b2_spec_config()
        pj_path = self._write_pj(tmp_pipelines, sc)
        pj_data = json.loads(pj_path.read_text())

        action = _check_spec_revise(sc, _now(), pj_data)
        assert action.send_to and "implementer1" in action.send_to, "B2 not triggered"
        action.expected_state = "SPEC_REVISE"

        with patch("engine.fsm_spec.send_to_agent_with_status",
                   return_value=SendResult.BUSY), \
             patch("engine.fsm_spec.notify_discord") as mock_notify:
            _apply_spec_action(pj_path, action, _now(), pj_data)

        result = json.loads(pj_path.read_text())
        sc_out = result["spec_config"]
        # pass 巻き戻し: 0 → +1 → -1 = 0
        assert sc_out.get("_self_review_pass", 0) == 0
        # _self_review_sent ロールバック (send_failure_rollback で None 復元)
        assert sc_out.get("_self_review_sent") is None
        # busy_since_key 初期化
        assert sc_out.get("_self_review_busy_since") is not None
        # "send failure" 通知が出ていない (BUSY なので)
        notified = "\n".join(str(c.args[0]) for c in mock_notify.call_args_list if c.args)
        assert "send failure" not in notified.lower()

    def test_b2_fail_consumes_retry_and_notifies(self, tmp_pipelines):
        """8: FAIL 受信 → counter 消費 + Discord "send failure"。"""
        from engine.fsm_spec import _check_spec_revise, _apply_spec_action
        from engine.backend_types import SendResult
        from unittest.mock import patch

        sc = self._b2_spec_config()
        pj_path = self._write_pj(tmp_pipelines, sc)
        pj_data = json.loads(pj_path.read_text())

        action = _check_spec_revise(sc, _now(), pj_data)
        action.expected_state = "SPEC_REVISE"

        with patch("engine.fsm_spec.send_to_agent_with_status",
                   return_value=SendResult.FAIL), \
             patch("engine.fsm_spec.notify_discord") as mock_notify:
            _apply_spec_action(pj_path, action, _now(), pj_data)

        result = json.loads(pj_path.read_text())
        sc_out = result["spec_config"]
        # FAIL ではカウンター消費 (+1 されたまま、巻き戻されない)
        assert sc_out.get("_self_review_pass") == 1
        # _self_review_sent は send_failure_rollback で None に巻き戻る
        assert sc_out.get("_self_review_sent") is None
        # FAIL では busy_since_key は設定されない (busy_agents 空)
        assert sc_out.get("_self_review_busy_since") is None
        # "send failure" 通知が出ている
        notified = "\n".join(str(c.args[0]) for c in mock_notify.call_args_list if c.args)
        assert "send failure" in notified.lower()

    def test_b2_fail_max_pauses(self, tmp_pipelines):
        """8 (続き): FAIL でカウンター枯渇後の次 tick で SPEC_PAUSED へ。"""
        from config import SPEC_REVISE_SELF_REVIEW_PASSES
        sc = self._b2_spec_config(
            _self_review_pass=SPEC_REVISE_SELF_REVIEW_PASSES,
        )
        # この sc で _check_spec_revise を直接呼ぶと B2 経路で current_pass >= MAX → SPEC_PAUSED
        action = _check_spec_revise(sc, _now(), _make_pipeline(spec_config=sc))
        assert action.next_state == "SPEC_PAUSED"
        pu = action.pipeline_updates or {}
        assert pu.get("paused_from") == "SPEC_REVISE"
        # busy_since もクリア
        assert pu.get("_self_review_busy_since") is None

    def test_b2_busy_then_ok_clears_busy_since(self, tmp_pipelines):
        """9: BUSY 1 回 → 次 tick OK で busy_since クリア + counter 進行。"""
        from engine.fsm_spec import _check_spec_revise, _apply_spec_action
        from engine.backend_types import SendResult
        from unittest.mock import patch

        # Tick 1: BUSY
        sc = self._b2_spec_config()
        pj_path = self._write_pj(tmp_pipelines, sc)
        pj_data = json.loads(pj_path.read_text())
        action = _check_spec_revise(sc, _now(), pj_data)
        action.expected_state = "SPEC_REVISE"
        with patch("engine.fsm_spec.send_to_agent_with_status",
                   return_value=SendResult.BUSY), \
             patch("engine.fsm_spec.notify_discord"):
            _apply_spec_action(pj_path, action, _now(), pj_data)
        sc_after_busy = json.loads(pj_path.read_text())["spec_config"]
        assert sc_after_busy.get("_self_review_busy_since") is not None
        assert sc_after_busy.get("_self_review_pass", 0) == 0

        # Tick 2: OK
        pj_data2 = json.loads(pj_path.read_text())
        sc2 = pj_data2["spec_config"]
        action2 = _check_spec_revise(sc2, _now(), pj_data2)
        action2.expected_state = "SPEC_REVISE"
        with patch("engine.fsm_spec.send_to_agent_with_status",
                   return_value=SendResult.OK), \
             patch("engine.fsm_spec.notify_discord"):
            _apply_spec_action(pj_path, action2, _now(), pj_data2)

        sc_out = json.loads(pj_path.read_text())["spec_config"]
        # _self_review_sent が今回の ISO 文字列にセットされている
        assert isinstance(sc_out.get("_self_review_sent"), str)
        # counter 進行
        assert sc_out.get("_self_review_pass") == 1
        # busy_since クリア
        assert sc_out.get("_self_review_busy_since") is None

    def test_b2_busy_escalation_after_threshold(self, tmp_pipelines):
        """10: BUSY 連続で BUSY_ESCALATION_SEC 経過 → FAIL 扱い + "busy escalation" 通知。"""
        from config import BUSY_ESCALATION_SEC
        from engine.fsm_spec import _check_spec_revise, _apply_spec_action
        from engine.backend_types import SendResult
        from unittest.mock import patch

        # 既にエスカレーション閾値分前に busy_since が立っている状態
        old_iso = (_now() - timedelta(seconds=BUSY_ESCALATION_SEC + 60)).isoformat()
        sc = self._b2_spec_config(_self_review_busy_since=old_iso)
        pj_path = self._write_pj(tmp_pipelines, sc)
        pj_data = json.loads(pj_path.read_text())
        action = _check_spec_revise(sc, _now(), pj_data)
        action.expected_state = "SPEC_REVISE"

        with patch("engine.fsm_spec.send_to_agent_with_status",
                   return_value=SendResult.BUSY), \
             patch("engine.fsm_spec.notify_discord") as mock_notify:
            _apply_spec_action(pj_path, action, _now(), pj_data)

        notified = "\n".join(str(c.args[0]) for c in mock_notify.call_args_list if c.args)
        # busy escalation 通知が出ている
        assert "busy escalation" in notified.lower()
        sc_out = json.loads(pj_path.read_text())["spec_config"]
        # FAIL 扱いに昇格 → counter は消費 (巻き戻されない)
        assert sc_out.get("_self_review_pass") == 1
        # _self_review_sent は rollback で None
        assert sc_out.get("_self_review_sent") is None
        # busy_agents が空 (escalated は failed に移る) → busy_since クリア
        assert sc_out.get("_self_review_busy_since") is None

    def test_b2_intermittent_busy_clears_busy_since(self, tmp_pipelines):
        """10b: BUSY → FAIL → 長時間経過 → BUSY のシーケンスで誤エスカレーション防止。"""
        from engine.fsm_spec import _check_spec_revise, _apply_spec_action
        from engine.backend_types import SendResult
        from unittest.mock import patch

        # Tick 1: BUSY
        sc = self._b2_spec_config()
        pj_path = self._write_pj(tmp_pipelines, sc)
        pj_data = json.loads(pj_path.read_text())
        action = _check_spec_revise(sc, _now(), pj_data)
        action.expected_state = "SPEC_REVISE"
        with patch("engine.fsm_spec.send_to_agent_with_status",
                   return_value=SendResult.BUSY), \
             patch("engine.fsm_spec.notify_discord"):
            _apply_spec_action(pj_path, action, _now(), pj_data)
        sc1 = json.loads(pj_path.read_text())["spec_config"]
        assert sc1.get("_self_review_busy_since") is not None

        # Tick 2: FAIL → busy_since はクリアされる (busy_agents 空)
        pj_data2 = json.loads(pj_path.read_text())
        sc2 = pj_data2["spec_config"]
        action2 = _check_spec_revise(sc2, _now(), pj_data2)
        action2.expected_state = "SPEC_REVISE"
        with patch("engine.fsm_spec.send_to_agent_with_status",
                   return_value=SendResult.FAIL), \
             patch("engine.fsm_spec.notify_discord"):
            _apply_spec_action(pj_path, action2, _now(), pj_data2)
        sc_after_fail = json.loads(pj_path.read_text())["spec_config"]
        # FAIL 後は busy_since がクリアされていること (誤エスカレーション防止の核心)
        assert sc_after_fail.get("_self_review_busy_since") is None

    def test_b2_busy_since_self_heal_on_corrupted_value(self, tmp_pipelines):
        """10c: busy_since_key が不正文字列でも escalation せず ISO に self-heal。"""
        from engine.fsm_spec import _check_spec_revise, _apply_spec_action
        from engine.backend_types import SendResult
        from unittest.mock import patch

        sc = self._b2_spec_config(_self_review_busy_since="corrupted-value")
        pj_path = self._write_pj(tmp_pipelines, sc)
        pj_data = json.loads(pj_path.read_text())
        action = _check_spec_revise(sc, _now(), pj_data)
        action.expected_state = "SPEC_REVISE"

        with patch("engine.fsm_spec.send_to_agent_with_status",
                   return_value=SendResult.BUSY), \
             patch("engine.fsm_spec.notify_discord") as mock_notify:
            _apply_spec_action(pj_path, action, _now(), pj_data)

        notified = "\n".join(str(c.args[0]) for c in mock_notify.call_args_list if c.args)
        # この tick では escalation は発火しない (elapsed=0 扱い)
        assert "busy escalation" not in notified.lower()
        sc_out = json.loads(pj_path.read_text())["spec_config"]
        # 値が ISO 文字列に self-heal されている
        from datetime import datetime as _dt
        healed = sc_out.get("_self_review_busy_since")
        assert isinstance(healed, str)
        # parse できる
        _dt.fromisoformat(healed)

    def test_b2_busy_since_self_heal_on_non_string(self, tmp_pipelines):
        """10c (続き): 非文字列値 (int) でも self-heal される。"""
        from engine.fsm_spec import _check_spec_revise, _apply_spec_action
        from engine.backend_types import SendResult
        from unittest.mock import patch

        sc = self._b2_spec_config(_self_review_busy_since=12345)
        pj_path = self._write_pj(tmp_pipelines, sc)
        pj_data = json.loads(pj_path.read_text())
        action = _check_spec_revise(sc, _now(), pj_data)
        action.expected_state = "SPEC_REVISE"

        with patch("engine.fsm_spec.send_to_agent_with_status",
                   return_value=SendResult.BUSY), \
             patch("engine.fsm_spec.notify_discord"):
            _apply_spec_action(pj_path, action, _now(), pj_data)

        sc_out = json.loads(pj_path.read_text())["spec_config"]
        from datetime import datetime as _dt
        healed = sc_out.get("_self_review_busy_since")
        assert isinstance(healed, str)
        _dt.fromisoformat(healed)

    def test_b2_busy_suppresses_discord_notify(self, tmp_pipelines):
        """10d: BUSY 時に applied_action.discord_notify が抑止される (timeout retry 経路)。"""
        from config import SPEC_BLOCK_TIMERS
        from engine.fsm_spec import _check_spec_revise, _apply_spec_action
        from engine.backend_types import SendResult
        from unittest.mock import patch

        # B 経路 (self_review timeout retry, discord_notify 付き) を発火させる
        sc = _make_spec_config(
            spec_implementer="implementer1",
            self_review_agent="implementer1",
            spec_path="docs/spec.md",
            current_rev="2",
            _self_review_sent=_past(SPEC_BLOCK_TIMERS["SPEC_REVIEW"] + 60),
            _self_review_response=None,
            _self_review_pass=0,
            _self_review_expected_ids=[c["id"] for c in DEFAULT_SELF_REVIEW_CHECKLIST],
        )
        pj_path = tmp_pipelines / "test-pj.json"
        write_pipeline(pj_path, _make_pipeline(state="SPEC_REVISE", spec_config=sc))
        pj_data = json.loads(pj_path.read_text())
        action = _check_spec_revise(sc, _now(), pj_data)
        # discord_notify が設定された timeout retry 経路であることを確認
        assert action.discord_notify is not None
        assert "self-review timeout" in action.discord_notify.lower()
        action.expected_state = "SPEC_REVISE"

        with patch("engine.fsm_spec.send_to_agent_with_status",
                   return_value=SendResult.BUSY), \
             patch("engine.fsm_spec.notify_discord") as mock_notify:
            _apply_spec_action(pj_path, action, _now(), pj_data)

        notified = "\n".join(str(c.args[0]) for c in mock_notify.call_args_list if c.args)
        # BUSY 時は "self-review timeout" 通知が抑止される
        assert "self-review timeout" not in notified.lower()

    def test_b_dateparse_fail_busy_rolls_back_pass(self, tmp_pipelines):
        """euler R2 P2: (B) self_review _self_review_sent 日付パース失敗の retry 分岐
        (fsm_spec.py:519-530) で BUSY 受信時に _self_review_pass が巻き戻ることを直接検証。
        この枝は euler R1 P1 の配線漏れの本命ポイントなので、最小回帰テストで固定する。
        """
        from engine.fsm_spec import _check_spec_revise, _apply_spec_action
        from engine.backend_types import SendResult
        from unittest.mock import patch

        sc = _make_spec_config(
            spec_implementer="implementer1",
            self_review_agent="implementer1",
            spec_path="docs/spec.md",
            current_rev="2",
            _self_review_sent="not-a-date",  # parse 失敗を強制
            _self_review_response=None,      # (A) ではなく (B) 経路へ
            _self_review_pass=0,             # < SPEC_REVISE_SELF_REVIEW_PASSES
            _self_review_expected_ids=[c["id"] for c in DEFAULT_SELF_REVIEW_CHECKLIST],
        )
        pj_path = tmp_pipelines / "test-pj.json"
        write_pipeline(pj_path, _make_pipeline(state="SPEC_REVISE", spec_config=sc))
        pj_data = json.loads(pj_path.read_text())

        action = _check_spec_revise(sc, _now(), pj_data)
        # 期待する分岐に着地していることを確認
        assert action.send_to and "implementer1" in action.send_to
        assert action.busy_counter_decrements == {"_self_review_pass": 1}
        assert action.busy_since_key == "_self_review_busy_since"
        action.expected_state = "SPEC_REVISE"

        with patch("engine.fsm_spec.send_to_agent_with_status",
                   return_value=SendResult.BUSY), \
             patch("engine.fsm_spec.notify_discord"):
            _apply_spec_action(pj_path, action, _now(), pj_data)

        sc_out = json.loads(pj_path.read_text())["spec_config"]
        # 0 → +1 → -1 = 0 (巻き戻し成立)
        assert sc_out.get("_self_review_pass", 0) == 0
        # send_failure_rollback により _self_review_sent は None に戻る
        # (元の "not-a-date" には戻らない: rollback dict が None 固定)
        assert sc_out.get("_self_review_sent") is None
        # busy_since が初期化されている
        assert sc_out.get("_self_review_busy_since") is not None

    def test_b2_fail_does_not_suppress_discord_notify(self, tmp_pipelines):
        """10d (続き): FAIL 時は通知が抑止されない (send failure は出る)。"""
        from config import SPEC_BLOCK_TIMERS
        from engine.fsm_spec import _check_spec_revise, _apply_spec_action
        from engine.backend_types import SendResult
        from unittest.mock import patch

        sc = _make_spec_config(
            spec_implementer="implementer1",
            self_review_agent="implementer1",
            spec_path="docs/spec.md",
            current_rev="2",
            _self_review_sent=_past(SPEC_BLOCK_TIMERS["SPEC_REVIEW"] + 60),
            _self_review_response=None,
            _self_review_pass=0,
            _self_review_expected_ids=[c["id"] for c in DEFAULT_SELF_REVIEW_CHECKLIST],
        )
        pj_path = tmp_pipelines / "test-pj.json"
        write_pipeline(pj_path, _make_pipeline(state="SPEC_REVISE", spec_config=sc))
        pj_data = json.loads(pj_path.read_text())
        action = _check_spec_revise(sc, _now(), pj_data)
        action.expected_state = "SPEC_REVISE"

        with patch("engine.fsm_spec.send_to_agent_with_status",
                   return_value=SendResult.FAIL), \
             patch("engine.fsm_spec.notify_discord") as mock_notify:
            _apply_spec_action(pj_path, action, _now(), pj_data)

        notified = "\n".join(str(c.args[0]) for c in mock_notify.call_args_list if c.args)
        # FAIL 時は send failure 通知 + (busy_agents 空なので) timeout retry 通知も出る
        assert "send failure" in notified.lower()


class TestB2SingleAgentAssertion:
    """単一エージェント assertion 検証 (pascal P1-1, spec test plan 12)。

    _apply_spec_action は DCL で action を再計算するため、check_transition_spec を
    パッチして二重宛先 + rollback 持ちのアクションを注入する。
    """

    def test_b2_multi_agent_send_with_rollback_raises(self, tmp_pipelines):
        """rollback 付きアクションを 2 エージェント宛に送るとアサーション。"""
        import pytest as _pytest
        from engine.fsm_spec import (
            _apply_spec_action, SpecTransitionAction,
        )
        from engine.backend_types import SendResult
        from unittest.mock import patch

        sc = _make_spec_config(
            spec_implementer="implementer1",
            self_review_agent="implementer1",
            spec_path="docs/spec.md",
            current_rev="2",
        )
        pj_path = tmp_pipelines / "test-pj.json"
        write_pipeline(pj_path, _make_pipeline(state="SPEC_REVISE", spec_config=sc))
        pj_data = json.loads(pj_path.read_text())

        # 二重宛先 + rollback 持ちのアクション（recompute 後にも残るよう patch で注入）
        bad_action = SpecTransitionAction(
            next_state=None,
            send_to={"implementer1": "msg1", "implementer2": "msg2"},
            send_failure_rollback={"_self_review_sent": None},
        )
        # expected_state は別オブジェクトで渡す
        outer_action = SpecTransitionAction(
            next_state=None,
            expected_state="SPEC_REVISE",
        )

        with patch("engine.fsm_spec.check_transition_spec",
                   return_value=bad_action), \
             patch("engine.fsm_spec.send_to_agent_with_status",
                   return_value=SendResult.OK), \
             patch("engine.fsm_spec.notify_discord"):
            with _pytest.raises(AssertionError, match="single agent"):
                _apply_spec_action(pj_path, outer_action, _now(), pj_data)
