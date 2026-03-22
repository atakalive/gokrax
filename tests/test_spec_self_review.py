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
        "gitlab": "atakalive/test-pj",
        "state": state,
        "spec_mode": True,
        "spec_config": spec_config if spec_config is not None else {},
        "enabled": True,
        "implementer": "kaneko",
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
            spec_implementer="kaneko",
            review_requests={
                "pascal": {"status": "pending", "sent_at": None, "timeout_at": None,
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
            spec_implementer="kaneko",
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
        assert "kaneko" in action.send_to

    def test_check_spec_revise_self_review_issues_no_double_send(self):
        """issues_found 差し戻し後、_revise_sent が更新されており (E) の初回送信が発火しない。"""
        sc = _make_spec_config(
            spec_implementer="kaneko",
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
            spec_implementer="kaneko",
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
            spec_implementer="kaneko",
            self_review_agent="pascal",
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
            spec_implementer="kaneko",
            self_review_agent="pascal",
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
            spec_implementer="kaneko",
            self_review_agent="pascal",
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
            spec_implementer="kaneko",
            self_review_agent="pascal",
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
            spec_implementer="kaneko",
            self_review_agent="pascal",
            spec_path="/repo/docs/test-spec.md",
            current_rev="1",
            review_requests={
                "pascal": {"status": "pending", "sent_at": None, "timeout_at": None,
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
            spec_implementer="kaneko",
            self_review_agent="pascal",
            spec_path="/repo/docs/test-spec.md",
            current_rev="1",
            review_requests={
                "pascal": {"status": "pending", "sent_at": None, "timeout_at": None,
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
            spec_implementer="kaneko",
            self_review_agent="pascal",
            spec_path="/repo/docs/test-spec.md",
            current_rev="1",
            self_review_checklist=custom,
            review_requests={
                "pascal": {"status": "pending", "sent_at": None, "timeout_at": None,
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
            spec_implementer="neumann",
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
            spec_implementer="neumann",
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
            spec_implementer="neumann",
        )
        result = _check_spec_revise(sc, _now(), _make_pipeline(spec_config=sc))
        assert result.next_state == "SPEC_PAUSED"
