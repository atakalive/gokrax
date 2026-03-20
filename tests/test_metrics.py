"""tests/test_metrics.py — Issue #81: レビュアーメトリクス計測テスト"""

import argparse
import json
import logging
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LOCAL_TZ = timezone(timedelta(hours=9))


# ── helpers ──────────────────────────────────────────────────────────────────

def _write_pipeline(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _base_pipeline(**overrides):
    data = {
        "project": "test-pj",
        "gitlab": "atakalive/test-pj",
        "state": "IDLE",
        "enabled": True,
        "implementer": "kaneko",
        "batch": [],
        "history": [],
        "review_mode": "standard",
        "created_at": "2025-01-01T00:00:00+09:00",
        "updated_at": "2025-01-01T00:00:00+09:00",
    }
    data.update(overrides)
    return data


def _make_batch_item(issue_num, commit=None, **kwargs):
    item = {
        "issue": issue_num, "title": f"Issue {issue_num}",
        "commit": commit, "cc_session_id": None,
        "design_reviews": {}, "code_reviews": {},
        "added_at": "2025-01-01T00:00:00+09:00",
    }
    item.update(kwargs)
    return item


# ── append_metric tests ─────────────────────────────────────────────────────

class TestAppendMetric:

    def test_writes_jsonl(self, tmp_path, monkeypatch):
        """JSONL に1行追記され、ts/event キーが含まれること"""
        import config
        metrics_file = tmp_path / "metrics.jsonl"
        monkeypatch.setattr(config, "METRICS_FILE", metrics_file)

        from pipeline_io import append_metric
        append_metric("test_event", pj="test-pj", issue=1)

        lines = metrics_file.read_text().strip().split("\n")
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["event"] == "test_event"
        assert record["pj"] == "test-pj"
        assert record["issue"] == 1
        assert "ts" in record
        # ts がオフセット付き ISO8601 であること
        assert "+" in record["ts"] or "Z" in record["ts"]

    def test_failure_logs_warning(self, tmp_path, monkeypatch, caplog):
        """書き込み不可パスでも例外が出ず warning がログされること"""
        import config
        # 存在しないディレクトリ内のパスを指定
        bad_path = tmp_path / "nonexistent" / "dir" / "metrics.jsonl"
        monkeypatch.setattr(config, "METRICS_FILE", bad_path)

        from pipeline_io import append_metric
        with caplog.at_level(logging.WARNING, logger="gokrax.metrics"):
            append_metric("test_event", pj="test-pj", issue=1)

        assert "metrics write failed" in caplog.text

    def test_concurrent_writes(self, tmp_path, monkeypatch):
        """2スレッド×100回 → 200行の有効 JSONL（行破損なし）"""
        import config
        metrics_file = tmp_path / "metrics.jsonl"
        monkeypatch.setattr(config, "METRICS_FILE", metrics_file)

        from pipeline_io import append_metric

        def writer(thread_id):
            for i in range(100):
                append_metric("concurrent", thread=thread_id, seq=i)

        t1 = threading.Thread(target=writer, args=(1,))
        t2 = threading.Thread(target=writer, args=(2,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        lines = metrics_file.read_text().strip().split("\n")
        assert len(lines) == 200
        for line in lines:
            record = json.loads(line)  # 各行が有効な JSON であること
            assert record["event"] == "concurrent"


# ── review_request metric tests ─────────────────────────────────────────────

class TestReviewRequestMetric:

    def test_metric_recorded(self):
        """notify_reviewers 呼び出しで append_metric が reviewer×issue 回呼ばれること"""
        import notify
        batch = [
            _make_batch_item(1),
            _make_batch_item(2),
        ]
        with patch("notify.send_to_agent", return_value=True) as mock_send:
            with patch("notify.fetch_issue_body", return_value="body"):
                with patch("pipeline_io.append_metric") as mock_metric:
                    notify.notify_reviewers(
                        "proj", "DESIGN_REVIEW", batch, "atakalive/proj",
                        review_mode="standard",
                    )

        # 各レビュアー × 各 Issue で呼ばれる
        req_calls = [c for c in mock_metric.call_args_list
                     if c.args[0] == "review_request"]
        import config
        reviewers = config.REVIEW_MODES["standard"]["members"]
        assert len(req_calls) == len(reviewers) * 2  # 2 issues × reviewers

        # 全呼び出しで phase="design" であること
        for call in req_calls:
            assert call.kwargs["phase"] == "design"

    def test_approve_skipped(self):
        """APPROVE 済み Issue はメトリクス記録されないこと"""
        import notify
        batch = [
            _make_batch_item(1, design_reviews={"pascal": {"verdict": "APPROVE", "at": ""}}),
            _make_batch_item(2),
        ]
        with patch("notify.send_to_agent", return_value=True):
            with patch("notify.fetch_issue_body", return_value="body"):
                with patch("pipeline_io.append_metric") as mock_metric:
                    notify.notify_reviewers(
                        "proj", "DESIGN_REVIEW", batch, "atakalive/proj",
                        review_mode="standard",
                    )

        # pascal の issue 1 はスキップされる
        pascal_calls = [c for c in mock_metric.call_args_list
                        if c.args[0] == "review_request"
                        and c.kwargs.get("reviewer") == "pascal"
                        and c.kwargs.get("issue") == 1]
        assert len(pascal_calls) == 0

    def test_send_failure_no_metric(self):
        """send_to_agent 失敗時はメトリクスが記録されないこと"""
        import notify
        batch = [_make_batch_item(1)]
        with patch("notify.send_to_agent", return_value=False):
            with patch("notify.fetch_issue_body", return_value="body"):
                with patch("pipeline_io.append_metric") as mock_metric:
                    notify.notify_reviewers(
                        "proj", "CODE_REVIEW", batch, "atakalive/proj",
                        review_mode="standard",
                    )

        req_calls = [c for c in mock_metric.call_args_list
                     if c.args[0] == "review_request"]
        assert len(req_calls) == 0


# ── review_response metric tests ────────────────────────────────────────────

class TestReviewResponseMetric:

    def _make_args(self, **kwargs):
        defaults = {
            "project": "test-pj",
            "issue": 1,
            "reviewer": "pascal",
            "verdict": "APPROVE",
            "summary": "",
            "force": False,
        }
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def test_metric_recorded(self, tmp_pipelines, monkeypatch):
        """cmd_review 呼び出しで review_response メトリクスが記録されること"""
        import config, pipeline_io
        monkeypatch.setattr(config, "PIPELINES_DIR", tmp_pipelines)
        monkeypatch.setattr(pipeline_io, "PIPELINES_DIR", tmp_pipelines)

        # 10秒前に CODE_REVIEW に遷移した history を持つ pipeline
        entered_at = (datetime.now(LOCAL_TZ) - timedelta(seconds=10)).isoformat()
        path = tmp_pipelines / "test-pj.json"
        _write_pipeline(path, _base_pipeline(
            state="CODE_REVIEW",
            code_revise_count=2,
            batch=[_make_batch_item(1, commit="abc123")],
            history=[{"from": "IMPLEMENTATION", "to": "CODE_REVIEW", "at": entered_at}],
        ))

        from gokrax import cmd_review

        with patch("pipeline_io.append_metric") as mock_metric, \
             patch("commands.dev._post_gitlab_note", return_value=False):
            cmd_review(self._make_args())

        mock_metric.assert_called_once()
        call_kwargs = mock_metric.call_args.kwargs
        assert mock_metric.call_args.args[0] == "review_response"
        assert call_kwargs["pj"] == "test-pj"
        assert call_kwargs["issue"] == 1
        assert call_kwargs["phase"] == "code"
        assert call_kwargs["reviewer"] == "pascal"
        assert call_kwargs["verdict"] == "APPROVE"
        assert call_kwargs["revise_cycle"] == 2
        assert isinstance(call_kwargs["latency_sec"], int)
        assert call_kwargs["latency_sec"] >= 0

    def test_skipped_no_metric(self, tmp_pipelines, monkeypatch):
        """同一レビュアー2回目はスキップされメトリクスも記録されないこと"""
        import config, pipeline_io
        monkeypatch.setattr(config, "PIPELINES_DIR", tmp_pipelines)
        monkeypatch.setattr(pipeline_io, "PIPELINES_DIR", tmp_pipelines)

        path = tmp_pipelines / "test-pj.json"
        # pascal が既にレビュー済み
        batch_item = _make_batch_item(1)
        batch_item["code_reviews"]["pascal"] = {"verdict": "P0", "at": ""}
        _write_pipeline(path, _base_pipeline(
            state="CODE_REVIEW",
            batch=[batch_item],
            history=[{"from": "IMPLEMENTATION", "to": "CODE_REVIEW",
                      "at": "2025-01-01T00:00:00+09:00"}],
        ))

        from gokrax import cmd_review

        with patch("pipeline_io.append_metric") as mock_metric, \
             patch("commands.dev._post_gitlab_note", return_value=False):
            cmd_review(self._make_args())

        mock_metric.assert_not_called()

    def test_latency_naive_datetime_fallback(self, tmp_pipelines, monkeypatch):
        """naive datetime の history でも latency_sec が算出されること"""
        import config, pipeline_io
        monkeypatch.setattr(config, "PIPELINES_DIR", tmp_pipelines)
        monkeypatch.setattr(pipeline_io, "PIPELINES_DIR", tmp_pipelines)

        # naive datetime（offset なし）の history
        naive_at = (datetime.now(LOCAL_TZ) - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%S")
        path = tmp_pipelines / "test-pj.json"
        _write_pipeline(path, _base_pipeline(
            state="DESIGN_REVIEW",
            batch=[_make_batch_item(1)],
            history=[{"from": "DESIGN_PLAN", "to": "DESIGN_REVIEW", "at": naive_at}],
        ))

        from gokrax import cmd_review

        with patch("pipeline_io.append_metric") as mock_metric, \
             patch("commands.dev._post_gitlab_note", return_value=False):
            cmd_review(self._make_args(verdict="P0"))

        call_kwargs = mock_metric.call_args.kwargs
        assert call_kwargs["phase"] == "design"
        assert isinstance(call_kwargs["latency_sec"], int)
        assert call_kwargs["latency_sec"] >= 25  # ~30秒前なので余裕を持つ
