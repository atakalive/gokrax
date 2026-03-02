"""tests/test_cmd_spec_review_submit.py — spec review-submit サブコマンドのテスト"""

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline_io import default_spec_config
from tests.conftest import write_pipeline


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_spec_config(**overrides):
    cfg = default_spec_config()
    cfg.update(overrides)
    return cfg


def _make_pipeline(state="SPEC_REVIEW", spec_mode=True, spec_config=None, **kwargs):
    data = {
        "project": "test-pj",
        "gitlab": "atakalive/test-pj",
        "state": state,
        "spec_mode": spec_mode,
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


def _review_requests(*reviewers):
    return {
        r: {"status": "pending", "sent_at": "2026-03-01T12:00:00+09:00",
            "timeout_at": "2026-03-01T12:30:00+09:00",
            "last_nudge_at": None, "response": None}
        for r in reviewers
    }


def _active_pipeline(**sc_overrides):
    sc = _make_spec_config(
        spec_path="docs/spec.md",
        spec_implementer="kaneko",
        review_requests=_review_requests("leibniz", "pascal"),
        **sc_overrides,
    )
    return _make_pipeline(state="SPEC_REVIEW", spec_mode=True, spec_config=sc)


def _args(**kwargs):
    return argparse.Namespace(**kwargs)


FENCED_YAML = """\
```yaml
verdict: P1
items:
  - id: C-1
    severity: critical
    section: "§6.2"
    title: "タイトル"
    description: "説明"
    suggestion: "修正案"
```
"""

RAW_YAML = """\
verdict: P1
items:
  - id: C-1
    severity: critical
    section: "§6.2"
    title: "タイトル"
    description: "説明"
    suggestion: "修正案"
"""


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestReviewSubmitNormal:
    """正常系テスト"""

    def test_fenced_yaml(self, tmp_pipelines, tmp_path):
        """フェンス付きYAMLからレビューを投入 → entries に書き込まれる"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _active_pipeline())

        review_file = tmp_path / "review.yaml"
        review_file.write_text(FENCED_YAML, encoding="utf-8")

        from devbar import cmd_spec_review_submit
        args = _args(project="test-pj", reviewer="leibniz", file=str(review_file))
        cmd_spec_review_submit(args)

        data = json.loads(path.read_text())
        sc = data["spec_config"]
        entry = sc["current_reviews"]["entries"]["leibniz"]
        assert entry["status"] == "received"
        assert entry["verdict"] == "P1"
        assert entry["parse_success"] is True
        assert len(entry["items"]) == 1
        assert entry["items"][0]["id"] == "C-1"
        assert entry["items"][0]["severity"] == "critical"
        assert entry["items"][0]["normalized_id"] == "leibniz:C-1"
        # review_requests も received に更新
        assert sc["review_requests"]["leibniz"]["status"] == "received"

    def test_raw_yaml_fallback(self, tmp_pipelines, tmp_path):
        """素のYAML → フォールバックで正常に書き込まれる"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _active_pipeline())

        review_file = tmp_path / "review.yaml"
        review_file.write_text(RAW_YAML, encoding="utf-8")

        from devbar import cmd_spec_review_submit
        args = _args(project="test-pj", reviewer="leibniz", file=str(review_file))
        cmd_spec_review_submit(args)

        data = json.loads(path.read_text())
        entry = data["spec_config"]["current_reviews"]["entries"]["leibniz"]
        assert entry["status"] == "received"
        assert entry["verdict"] == "P1"
        assert len(entry["items"]) == 1


class TestReviewSubmitIdempotency:
    """冪等性テスト"""

    def test_duplicate_skip(self, tmp_pipelines, tmp_path, capsys):
        """同じレビュアーが2回投入 → 2回目は skipping"""
        pipeline = _active_pipeline()
        # 1回目の結果を手動で設定
        sc = pipeline["spec_config"]
        sc["current_reviews"] = {
            "entries": {
                "leibniz": {
                    "status": "received",
                    "verdict": "P1",
                    "items": [],
                    "raw_text": "...",
                    "parse_success": True,
                },
            },
        }
        sc["review_requests"]["leibniz"]["status"] = "received"

        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, pipeline)

        review_file = tmp_path / "review.yaml"
        review_file.write_text(FENCED_YAML, encoding="utf-8")

        from devbar import cmd_spec_review_submit
        args = _args(project="test-pj", reviewer="leibniz", file=str(review_file))
        cmd_spec_review_submit(args)

        captured = capsys.readouterr()
        assert "already submitted, skipping" in captured.out


class TestReviewSubmitErrors:
    """エラー系テスト"""

    def test_wrong_state(self, tmp_pipelines, tmp_path):
        """SPEC_REVIEW 以外の状態 → SystemExit"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _make_pipeline(
            state="IDLE",
            spec_config=_make_spec_config(
                review_requests=_review_requests("leibniz"),
            ),
        ))

        review_file = tmp_path / "review.yaml"
        review_file.write_text(FENCED_YAML, encoding="utf-8")

        from devbar import cmd_spec_review_submit
        args = _args(project="test-pj", reviewer="leibniz", file=str(review_file))
        with pytest.raises(SystemExit, match="Not in SPEC_REVIEW state"):
            cmd_spec_review_submit(args)

    def test_invalid_reviewer(self, tmp_pipelines, tmp_path):
        """review_requests にないレビュアー名 → SystemExit"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _active_pipeline())

        review_file = tmp_path / "review.yaml"
        review_file.write_text(FENCED_YAML, encoding="utf-8")

        from devbar import cmd_spec_review_submit
        args = _args(project="test-pj", reviewer="unknown", file=str(review_file))
        with pytest.raises(SystemExit, match="not in review_requests"):
            cmd_spec_review_submit(args)

    def test_parse_failure(self, tmp_pipelines, tmp_path):
        """YAMLとして無効なファイル → SystemExit"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _active_pipeline())

        review_file = tmp_path / "review.yaml"
        review_file.write_text("this is not valid yaml: [[[", encoding="utf-8")

        from devbar import cmd_spec_review_submit
        args = _args(project="test-pj", reviewer="leibniz", file=str(review_file))
        with pytest.raises(SystemExit, match="Failed to parse review YAML"):
            cmd_spec_review_submit(args)

    def test_file_not_found(self, tmp_pipelines):
        """存在しないファイルパス → SystemExit"""
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, _active_pipeline())

        from devbar import cmd_spec_review_submit
        args = _args(project="test-pj", reviewer="leibniz", file="/nonexistent/review.yaml")
        with pytest.raises(SystemExit, match="File not found"):
            cmd_spec_review_submit(args)


class TestReviewSubmitArchive:
    """アーカイブ保存テスト"""

    def test_archive_saved(self, tmp_pipelines, tmp_path):
        """pipelines_dir が設定されている場合、アーカイブファイルがコピーされる"""
        archive_dir = tmp_path / "archives"
        archive_dir.mkdir()

        pipeline = _active_pipeline(pipelines_dir=str(archive_dir))
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, pipeline)

        review_file = tmp_path / "review.yaml"
        review_file.write_text(FENCED_YAML, encoding="utf-8")

        from devbar import cmd_spec_review_submit
        args = _args(project="test-pj", reviewer="leibniz", file=str(review_file))
        cmd_spec_review_submit(args)

        # アーカイブファイルが存在すること
        archives = list(archive_dir.glob("*_leibniz_spec_rev1.yaml"))
        assert len(archives) == 1
        # 内容が元ファイルと一致
        assert archives[0].read_text(encoding="utf-8") == FENCED_YAML
        # パーミッション 0600
        assert oct(archives[0].stat().st_mode & 0o777) == "0o600"

    def test_archive_failure_warning(self, tmp_pipelines, tmp_path, capsys):
        """アーカイブ保存の OSError → warning のみで正常終了"""
        # 存在しないディレクトリを pipelines_dir に指定
        pipeline = _active_pipeline(pipelines_dir="/nonexistent/dir")
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, pipeline)

        review_file = tmp_path / "review.yaml"
        review_file.write_text(FENCED_YAML, encoding="utf-8")

        from devbar import cmd_spec_review_submit
        args = _args(project="test-pj", reviewer="leibniz", file=str(review_file))
        # SystemExit が発生しないこと（正常終了）
        cmd_spec_review_submit(args)

        captured = capsys.readouterr()
        assert "warning: archive failed" in captured.out
        # pipeline への書き込みは成功している
        data = json.loads(path.read_text())
        entry = data["spec_config"]["current_reviews"]["entries"]["leibniz"]
        assert entry["status"] == "received"
