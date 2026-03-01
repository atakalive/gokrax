"""tests/test_spec_issue.py — spec_issue.py + watchdog 統合テスト"""

import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from spec_issue import (
    build_issue_suggestion_prompt,
    parse_issue_suggestion_response,
    build_issue_plan_prompt,
    parse_issue_plan_response,
    build_queue_plan_prompt,
    parse_queue_plan_response,
)
from watchdog import (
    SpecTransitionAction,
    _check_issue_suggestion,
    _check_issue_plan,
    _check_queue_plan,
)
import config

JST = timezone(timedelta(hours=9))


def _now():
    return datetime(2026, 3, 1, 12, 0, 0, tzinfo=JST)


def _yaml_block(content: str) -> str:
    return f"```yaml\n{content}\n```"


# ===========================================================================
# parse_issue_suggestion_response
# ===========================================================================

class TestParseIssueSuggestionResponse:
    # 1. 正常YAML（phases + issues） → dict 返却
    def test_valid(self):
        raw = _yaml_block(
            "phases:\n"
            "  - name: Phase 1\n"
            "    issues:\n"
            "      - title: Implement foo\n"
            "        files: []\n"
        )
        result = parse_issue_suggestion_response(raw)
        assert result is not None
        assert "phases" in result
        assert result["phases"][0]["name"] == "Phase 1"
        assert result["phases"][0]["issues"][0]["title"] == "Implement foo"

    # 2. YAML ブロックなし → None
    def test_no_yaml_block(self):
        assert parse_issue_suggestion_response("no yaml here") is None

    # 3. phases 欠落 → None
    def test_missing_phases_key(self):
        raw = _yaml_block("other_key: value\n")
        assert parse_issue_suggestion_response(raw) is None

    # 4. phases が list でない → None
    def test_phases_not_list(self):
        raw = _yaml_block("phases: not_a_list\n")
        assert parse_issue_suggestion_response(raw) is None

    # 5. issue に title 欠落 → None
    def test_issue_missing_title(self):
        raw = _yaml_block(
            "phases:\n"
            "  - name: Phase 1\n"
            "    issues:\n"
            "      - files: [foo.py]\n"
        )
        assert parse_issue_suggestion_response(raw) is None

    # 6. 空の phases リスト → None
    def test_empty_phases_list(self):
        raw = _yaml_block("phases: []\n")
        assert parse_issue_suggestion_response(raw) is None

    # 7. phase 内の issues が空リスト → None
    def test_empty_issues_list(self):
        raw = _yaml_block(
            "phases:\n"
            "  - name: Phase 1\n"
            "    issues: []\n"
        )
        assert parse_issue_suggestion_response(raw) is None

    # 8. issue の title が空文字列 → None
    def test_issue_title_empty_string(self):
        raw = _yaml_block(
            "phases:\n"
            "  - name: Phase 1\n"
            "    issues:\n"
            '      - title: ""\n'
        )
        assert parse_issue_suggestion_response(raw) is None

    # 9. phase が dict でない（文字列等） → None
    def test_phase_not_dict(self):
        raw = _yaml_block(
            "phases:\n"
            "  - just a string\n"
        )
        assert parse_issue_suggestion_response(raw) is None


# ===========================================================================
# parse_issue_plan_response
# ===========================================================================

class TestParseIssuePlanResponse:
    # 10. 正常（status:done, created_issues:[1,2,3]） → dict
    def test_valid(self):
        raw = _yaml_block("status: done\ncreated_issues:\n  - 1\n  - 2\n  - 3\n")
        result = parse_issue_plan_response(raw)
        assert result is not None
        assert result["status"] == "done"
        assert result["created_issues"] == [1, 2, 3]

    # 11. status != "done" → None
    def test_status_not_done(self):
        raw = _yaml_block("status: pending\ncreated_issues:\n  - 1\n")
        assert parse_issue_plan_response(raw) is None

    # 12. created_issues が空リスト → None
    def test_empty_created_issues(self):
        raw = _yaml_block("status: done\ncreated_issues: []\n")
        assert parse_issue_plan_response(raw) is None

    # 13. created_issues に数値文字列 "51" → None（int のみ）
    def test_string_issue_number(self):
        raw = _yaml_block('status: done\ncreated_issues:\n  - "51"\n')
        assert parse_issue_plan_response(raw) is None

    # 14. YAML なし → None
    def test_no_yaml(self):
        assert parse_issue_plan_response("done, created 51, 52") is None


# ===========================================================================
# parse_queue_plan_response
# ===========================================================================

class TestParseQueuePlanResponse:
    # 15. 正常（status:done, batches:3, queue_file:"/path/to/file"） → dict
    def test_valid(self):
        raw = _yaml_block('status: done\nbatches: 3\nqueue_file: "/path/to/queue.txt"\n')
        result = parse_queue_plan_response(raw)
        assert result is not None
        assert result["batches"] == 3
        assert result["queue_file"] == "/path/to/queue.txt"

    # 16. batches < 1 → None
    def test_batches_zero(self):
        raw = _yaml_block('status: done\nbatches: 0\nqueue_file: "/path"\n')
        assert parse_queue_plan_response(raw) is None

    # 17. batches が float → None
    def test_batches_float(self):
        raw = _yaml_block('status: done\nbatches: 1.5\nqueue_file: "/path"\n')
        assert parse_queue_plan_response(raw) is None

    # 18. batches が bool True → None
    def test_batches_bool(self):
        raw = _yaml_block('status: done\nbatches: true\nqueue_file: "/path"\n')
        assert parse_queue_plan_response(raw) is None

    # 19. queue_file 空文字 → None
    def test_queue_file_empty(self):
        raw = _yaml_block('status: done\nbatches: 2\nqueue_file: ""\n')
        assert parse_queue_plan_response(raw) is None


# ===========================================================================
# build_*_prompt
# ===========================================================================

class TestBuildPrompts:
    # 20. build_issue_suggestion_prompt: spec_path/rev/project が埋め込まれること
    def test_build_issue_suggestion_prompt(self):
        sc = {"spec_path": "docs/spec.md", "current_rev": "3"}
        data = {"project": "devbar"}
        prompt = build_issue_suggestion_prompt(sc, data)
        assert "docs/spec.md" in prompt
        assert "rev3" in prompt
        assert "devbar" in prompt
        assert "phases" in prompt  # YAML テンプレート含む

    # 21. build_issue_plan_prompt: issue_suggestions の各 reviewer 提案が含まれること + 統合指示
    def test_build_issue_plan_prompt(self):
        sc = {
            "spec_path": "docs/spec.md",
            "current_rev": "2",
            "issue_suggestions": {
                "leibniz": {"phases": [{"name": "P1", "issues": [{"title": "Foo"}]}]},
                "dijkstra": {"phases": [{"name": "P1", "issues": [{"title": "Bar"}]}]},
            },
        }
        data = {"project": "myproj"}
        prompt = build_issue_plan_prompt(sc, data)
        assert "leibniz" in prompt
        assert "dijkstra" in prompt
        assert "重複を排除" in prompt
        assert "統合" in prompt
        assert "myproj" in prompt
        assert "docs/spec.md" in prompt

    # 22. build_queue_plan_prompt: created_issues/QUEUE_FILE 絶対パス/スペース区切りフォーマット説明
    def test_build_queue_plan_prompt(self):
        sc = {
            "spec_path": "docs/spec.md",
            "created_issues": [51, 52, 53],
        }
        data = {"project": "devbar"}
        prompt = build_queue_plan_prompt(sc, data)
        queue_path = str(config.QUEUE_FILE)
        assert queue_path in prompt
        assert "51 52 53" in prompt
        # スペース区切りフォーマット説明
        assert "issue_nums" in prompt


# ===========================================================================
# watchdog 統合: _check_issue_suggestion
# ===========================================================================

class TestCheckIssueSuggestion:
    def _base_sc(self):
        return {
            "spec_path": "docs/spec.md",
            "current_rev": "1",
            "review_requests": {
                "leibniz": {"status": "pending", "sent_at": None, "timeout_at": None,
                            "last_nudge_at": None, "response": None},
            },
            "current_reviews": {"entries": {}},
        }

    # 23. 未送信 reviewer → send_to にプロンプトが積まれる
    def test_unsent_reviewer_sends_prompt(self):
        sc = self._base_sc()
        action = _check_issue_suggestion(sc, _now(), {"project": "devbar"})
        assert action.send_to is not None
        assert "leibniz" in action.send_to
        assert len(action.send_to["leibniz"]) > 0
        rr_patch = action.pipeline_updates["review_requests_patch"]
        assert "sent_at" in rr_patch["leibniz"]
        assert "timeout_at" in rr_patch["leibniz"]

    # 24. タイムアウト → rr_patch に status:timeout（既存フィールド保持）
    def test_timeout_sets_status(self):
        past = _now() - timedelta(seconds=config.SPEC_ISSUE_SUGGESTION_TIMEOUT_SEC + 10)
        sc = self._base_sc()
        sc["review_requests"]["leibniz"].update({
            "sent_at": past.isoformat(),
            "timeout_at": past.isoformat(),  # 既に超過
        })
        action = _check_issue_suggestion(sc, _now(), {"project": "devbar"})
        rr_patch = action.pipeline_updates["review_requests_patch"]
        assert rr_patch["leibniz"]["status"] == "timeout"
        assert "sent_at" in rr_patch["leibniz"]
        assert "timeout_at" in rr_patch["leibniz"]

    # 25. パース成功 → rr_patch に status:received（2人中1人が応答した中間状態）
    def test_parse_success_received(self):
        sc = self._base_sc()
        # 2人目のレビュアー（未応答）を追加 → all_complete=False の中間状態を作る
        sc["review_requests"]["dijkstra"] = {
            "status": "pending", "sent_at": _now().isoformat(),
            "timeout_at": (_now() + timedelta(seconds=600)).isoformat(),
            "last_nudge_at": None, "response": None,
        }
        sc["review_requests"]["leibniz"]["sent_at"] = _now().isoformat()
        raw = _yaml_block(
            "phases:\n"
            "  - name: Phase 1\n"
            "    issues:\n"
            "      - title: Implement auth\n"
        )
        sc["current_reviews"]["entries"]["leibniz"] = {
            "status": "received",
            "raw_text": raw,
        }
        action = _check_issue_suggestion(sc, _now(), {"project": "devbar"})
        # dijkstra はまだ pending → all_complete=False → review_requests_patch に "received" が残る
        rr_patch = action.pipeline_updates["review_requests_patch"]
        assert rr_patch["leibniz"]["status"] == "received"

    # 26. パース失敗 → rr_patch に status:parse_failed
    def test_parse_failure_parse_failed(self):
        sc = self._base_sc()
        sc["review_requests"]["leibniz"]["sent_at"] = _now().isoformat()
        sc["current_reviews"]["entries"]["leibniz"] = {
            "status": "received",
            "raw_text": "this is not valid yaml suggestion",
        }
        action = _check_issue_suggestion(sc, _now(), {"project": "devbar"})
        rr_patch = action.pipeline_updates["review_requests_patch"]
        assert rr_patch["leibniz"]["status"] == "parse_failed"

    # 27. 全員応答（1件有効）→ ISSUE_PLAN 遷移 + review_requests 完全リセット
    def test_all_responded_valid_transitions_to_issue_plan(self):
        sc = self._base_sc()
        raw = _yaml_block(
            "phases:\n"
            "  - name: Phase 1\n"
            "    issues:\n"
            "      - title: Implement foo\n"
        )
        sc["review_requests"]["leibniz"]["sent_at"] = _now().isoformat()
        sc["current_reviews"]["entries"]["leibniz"] = {
            "status": "received",
            "raw_text": raw,
        }
        action = _check_issue_suggestion(sc, _now(), {"project": "devbar"})
        assert action.next_state == "ISSUE_PLAN"
        assert "[Spec] Issue分割提案回収完了" in action.discord_notify
        # review_requests 完全リセット確認
        reset = action.pipeline_updates["review_requests_patch"]["leibniz"]
        assert reset["status"] == "pending"
        assert reset["sent_at"] is None
        assert reset["timeout_at"] is None
        assert reset["last_nudge_at"] is None
        assert reset["response"] is None

    # 28. 全員応答（0件有効）→ SPEC_PAUSED
    def test_all_responded_none_valid_transitions_to_paused(self):
        sc = self._base_sc()
        sc["review_requests"]["leibniz"]["sent_at"] = _now().isoformat()
        sc["current_reviews"]["entries"]["leibniz"] = {
            "status": "received",
            "raw_text": "not a valid suggestion",
        }
        action = _check_issue_suggestion(sc, _now(), {"project": "devbar"})
        assert action.next_state == "SPEC_PAUSED"
        assert "有効応答なし" in action.discord_notify
        assert action.pipeline_updates.get("paused_from") == "ISSUE_SUGGESTION"


# ===========================================================================
# watchdog 統合: _check_issue_plan
# ===========================================================================

class TestCheckIssuePlan:
    def _base_sc(self, **kwargs):
        sc = {
            "spec_path": "docs/spec.md",
            "current_rev": "1",
            "spec_implementer": "hanfei",
            "issue_suggestions": {
                "leibniz": {"phases": [{"name": "P1", "issues": [{"title": "Foo"}]}]},
            },
            "retry_counts": {},
        }
        sc.update(kwargs)
        return sc

    # 29. 未送信 → send_to に implementer プロンプト
    def test_unsent_sends_prompt(self):
        sc = self._base_sc()
        action = _check_issue_plan(sc, _now(), {"project": "devbar"})
        assert action.send_to is not None
        assert "hanfei" in action.send_to
        assert action.pipeline_updates.get("_issue_plan_sent") is not None

    # 30. 応答あり（正常, no_queue=false）→ QUEUE_PLAN 遷移 + created_issues 設定
    def test_response_valid_no_queue_false(self):
        raw = _yaml_block("status: done\ncreated_issues:\n  - 51\n  - 52\n")
        sc = self._base_sc(_issue_plan_response=raw, _issue_plan_sent=_now().isoformat())
        action = _check_issue_plan(sc, _now(), {"project": "devbar"})
        assert action.next_state == "QUEUE_PLAN"
        assert action.pipeline_updates["created_issues"] == [51, 52]
        assert action.pipeline_updates.get("_issue_plan_response") is None
        assert "2件" in action.discord_notify

    # 31. 応答あり（正常, no_queue=true）→ SPEC_DONE 遷移 + created_issues 設定
    def test_response_valid_no_queue_true(self):
        raw = _yaml_block("status: done\ncreated_issues:\n  - 51\n")
        sc = self._base_sc(
            _issue_plan_response=raw,
            _issue_plan_sent=_now().isoformat(),
            no_queue=True,
        )
        action = _check_issue_plan(sc, _now(), {"project": "devbar"})
        assert action.next_state == "SPEC_DONE"
        assert action.pipeline_updates["created_issues"] == [51]

    # 32. 応答あり（パース失敗）→ SPEC_PAUSED + discord_notify
    def test_response_parse_failure(self):
        sc = self._base_sc(
            _issue_plan_response="invalid response",
            _issue_plan_sent=_now().isoformat(),
        )
        action = _check_issue_plan(sc, _now(), {"project": "devbar"})
        assert action.next_state == "SPEC_PAUSED"
        assert "パース失敗" in action.discord_notify
        assert action.pipeline_updates.get("paused_from") == "ISSUE_PLAN"

    # 33. タイムアウト + リトライ → _issue_plan_sent リセット + discord_notify
    def test_timeout_retry(self):
        old_time = _now() - timedelta(seconds=config.SPEC_ISSUE_PLAN_TIMEOUT_SEC + 10)
        sc = self._base_sc(
            _issue_plan_sent=old_time.isoformat(),
            retry_counts={"ISSUE_PLAN": 0},
        )
        action = _check_issue_plan(sc, _now(), {"project": "devbar"})
        assert action.next_state is None
        assert action.pipeline_updates.get("_issue_plan_sent") is None
        assert "タイムアウト" in action.discord_notify
        assert "retry" in action.discord_notify

    # 34. タイムアウト + MAX_SPEC_RETRIES 超過 → SPEC_PAUSED + discord_notify
    def test_timeout_max_retries(self):
        old_time = _now() - timedelta(seconds=config.SPEC_ISSUE_PLAN_TIMEOUT_SEC + 10)
        sc = self._base_sc(
            _issue_plan_sent=old_time.isoformat(),
            retry_counts={"ISSUE_PLAN": config.MAX_SPEC_RETRIES},
        )
        action = _check_issue_plan(sc, _now(), {"project": "devbar"})
        assert action.next_state == "SPEC_PAUSED"
        assert "タイムアウト" in action.discord_notify


# ===========================================================================
# watchdog 統合: _check_queue_plan
# ===========================================================================

class TestCheckQueuePlan:
    def _base_sc(self, **kwargs):
        sc = {
            "spec_path": "docs/spec.md",
            "spec_implementer": "hanfei",
            "created_issues": [51, 52, 53],
            "retry_counts": {},
        }
        sc.update(kwargs)
        return sc

    # 35. 未送信 → send_to に implementer プロンプト
    def test_unsent_sends_prompt(self):
        sc = self._base_sc()
        action = _check_queue_plan(sc, _now(), {"project": "devbar"})
        assert action.send_to is not None
        assert "hanfei" in action.send_to
        assert action.pipeline_updates.get("_queue_plan_sent") is not None

    # 36. 応答あり（正常）→ SPEC_DONE 遷移
    def test_response_valid(self):
        queue_path = str(config.QUEUE_FILE)
        raw = _yaml_block(
            f'status: done\nbatches: 3\nqueue_file: "{queue_path}"\n'
        )
        sc = self._base_sc(
            _queue_plan_response=raw,
            _queue_plan_sent=_now().isoformat(),
        )
        action = _check_queue_plan(sc, _now(), {"project": "devbar"})
        assert action.next_state == "SPEC_DONE"
        assert "3バッチ" in action.discord_notify
        assert action.pipeline_updates.get("_queue_plan_response") is None

    # 37. 応答あり（パース失敗）→ SPEC_PAUSED + discord_notify
    def test_response_parse_failure(self):
        sc = self._base_sc(
            _queue_plan_response="not valid",
            _queue_plan_sent=_now().isoformat(),
        )
        action = _check_queue_plan(sc, _now(), {"project": "devbar"})
        assert action.next_state == "SPEC_PAUSED"
        assert "パース失敗" in action.discord_notify
        assert action.pipeline_updates.get("paused_from") == "QUEUE_PLAN"

    # 38. 複数reviewer・受領と完了が別tick（Leibniz P0再現テスト）
    def test_multi_tick_issue_suggestions_preserved(self):
        """tick1でpascalがreceived→永続化、tick2でleibnizがtimeout→all_complete。
        issue_suggestionsにpascalの提案が残っていること。"""
        raw = _yaml_block(
            "phases:\n"
            "  - name: Phase 1\n"
            "    issues:\n"
            "      - title: Implement foo\n"
        )
        # tick1: pascal received, leibniz still pending
        sc = {
            "review_requests": {
                "pascal": {
                    "status": "pending", "sent_at": _now().isoformat(),
                    "timeout_at": (_now() + timedelta(seconds=600)).isoformat(),
                    "last_nudge_at": None, "response": None,
                },
                "leibniz": {
                    "status": "pending", "sent_at": _now().isoformat(),
                    "timeout_at": (_now() + timedelta(seconds=600)).isoformat(),
                    "last_nudge_at": None, "response": None,
                },
            },
            "current_reviews": {
                "entries": {
                    "pascal": {"status": "received", "raw_text": raw},
                },
            },
            "spec_path": "docs/spec.md",
            "current_rev": "1",
        }
        action1 = _check_issue_suggestion(sc, _now(), {"project": "devbar"})
        # pascal は received になり、issue_suggestions に格納される
        assert action1.pipeline_updates["issue_suggestions"]["pascal"] is not None
        assert action1.next_state is None  # leibniz がまだ pending

        # tick2: pascal の提案は永続化済み(spec_configに反映)、leibniz がtimeout
        sc2 = {
            "review_requests": {
                "pascal": {
                    "status": "received",  # tick1 で更新済み
                    "sent_at": _now().isoformat(),
                    "timeout_at": (_now() - timedelta(seconds=1)).isoformat(),
                    "last_nudge_at": None, "response": None,
                },
                "leibniz": {
                    "status": "pending", "sent_at": _now().isoformat(),
                    "timeout_at": (_now() - timedelta(seconds=1)).isoformat(),  # expired
                    "last_nudge_at": None, "response": None,
                },
            },
            "current_reviews": {"entries": {}},
            # tick1 で永続化された issue_suggestions
            "issue_suggestions": {"pascal": {"phases": [{"name": "Phase 1", "issues": [{"title": "Implement foo"}]}]}},
            "spec_path": "docs/spec.md",
            "current_rev": "1",
        }
        action2 = _check_issue_suggestion(sc2, _now(), {"project": "devbar"})
        # all_complete (pascal=received, leibniz=timeout) → ISSUE_PLAN
        assert action2.next_state == "ISSUE_PLAN"
        # pascal の提案が保持されていること
        assert "pascal" in action2.pipeline_updates["issue_suggestions"]
