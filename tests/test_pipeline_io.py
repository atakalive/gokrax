"""tests/test_pipeline_io.py — pipeline_io のユニットテスト + 結合テスト"""

import json
import os
import threading
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def write_pipeline(path: Path, data: dict):
    """テスト用: パイプラインJSONを書き込む。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class TestUpdatePipelineConcurrency:
    """4スレッド並列 update_pipeline → 全キー残存"""

    def test_parallel_writes(self, tmp_pipelines, sample_pipeline):
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)

        from pipeline_io import update_pipeline

        errors = []

        def writer(key, value):
            try:
                def cb(data):
                    data[key] = value
                update_pipeline(path, cb)
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(4):
            t = threading.Thread(target=writer, args=(f"key_{i}", f"val_{i}"))
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors in threads: {errors}"

        with open(path) as f:
            data = json.load(f)
        for i in range(4):
            assert data[f"key_{i}"] == f"val_{i}", f"key_{i} missing or wrong"


class TestUpdatePipelineException:
    """callback内例外 → ロック解放 + ファイル破損なし"""

    def test_callback_exception_releases_lock(self, tmp_pipelines, sample_pipeline):
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)

        from pipeline_io import update_pipeline, load_pipeline

        def bad_callback(data):
            data["should_not_persist"] = True
            raise ValueError("intentional error")

        with pytest.raises(ValueError, match="intentional error"):
            update_pipeline(path, bad_callback)

        # ファイルは壊れていない（元のデータのまま）
        data = load_pipeline(path)
        assert data["project"] == "test-pj"
        assert "should_not_persist" not in data

        # ロック解放確認: 次の update が成功する
        def ok_callback(data):
            data["recovered"] = True

        update_pipeline(path, ok_callback)
        data = load_pipeline(path)
        assert data["recovered"] is True


class TestAtomicWrite:
    """atomic write テスト"""

    def test_no_partial_write(self, tmp_pipelines, sample_pipeline):
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)

        from pipeline_io import save_pipeline, load_pipeline

        sample_pipeline["new_field"] = "test_value"
        save_pipeline(path, sample_pipeline)

        data = load_pipeline(path)
        assert data["new_field"] == "test_value"
        assert "updated_at" in data

    def test_tmp_files_cleaned_on_success(self, tmp_pipelines, sample_pipeline):
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)

        from pipeline_io import save_pipeline

        save_pipeline(path, sample_pipeline)

        tmp_files = list(tmp_pipelines.glob("*.tmp"))
        assert tmp_files == [], f"Leftover tmp files: {tmp_files}"


class TestHelpers:
    """add_history / find_issue / now_iso"""

    def test_add_history(self):
        from pipeline_io import add_history

        data = {}
        add_history(data, "IDLE", "DESIGN_PLAN", actor="cli")
        assert len(data["history"]) == 1
        assert data["history"][0]["from"] == "IDLE"
        assert data["history"][0]["to"] == "DESIGN_PLAN"
        assert data["history"][0]["actor"] == "cli"
        assert "at" in data["history"][0]

    def test_find_issue_found(self):
        from pipeline_io import find_issue

        batch = [{"issue": 1, "title": "a"}, {"issue": 2, "title": "b"}]
        assert find_issue(batch, 2)["title"] == "b"

    def test_find_issue_not_found(self):
        from pipeline_io import find_issue

        batch = [{"issue": 1}]
        assert find_issue(batch, 99) is None

    def test_now_iso_format(self):
        from pipeline_io import now_iso

        ts = now_iso()
        assert "T" in ts
        assert "+" in ts or "-" in ts  # timezone info


class TestDevbarCLIIntegration:
    """gokrax.py の全サブコマンドが pipeline_io 経由で正常動作"""

    def _run(self, *cmd_args, pipelines_dir=None):
        cli = str(ROOT / "gokrax.py")
        env = os.environ.copy()
        if pipelines_dir:
            env["GOKRAX_PIPELINES_DIR"] = str(pipelines_dir)
        # DRY_RUN mode to avoid actual glab/git commands
        env["GOKRAX_DRY_RUN"] = "1"
        result = subprocess.run(
            [sys.executable, cli] + list(cmd_args),
            capture_output=True, text=True, timeout=10, env=env,
        )
        return result

    def test_init_and_status(self, tmp_pipelines):
        r = self._run("init", "--project", "integ-test", pipelines_dir=tmp_pipelines)
        assert r.returncode == 0, r.stderr
        assert "Created" in r.stdout

        path = tmp_pipelines / "integ-test.json"
        assert path.exists()

        r = self._run("status", pipelines_dir=tmp_pipelines)
        assert r.returncode == 0
        assert "integ-test" in r.stdout

    def test_enable_disable(self, tmp_pipelines, sample_pipeline):
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)

        r = self._run("enable", "--project", "test-pj", pipelines_dir=tmp_pipelines)
        assert r.returncode == 0
        with open(path) as f:
            assert json.load(f)["enabled"] is True

        r = self._run("disable", "--project", "test-pj", pipelines_dir=tmp_pipelines)
        assert r.returncode == 0
        with open(path) as f:
            assert json.load(f)["enabled"] is False

    def test_triage(self, tmp_pipelines, sample_pipeline):
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)

        r = self._run("triage", "--project", "test-pj", "--issue", "42", "--title", "Test Issue",
                       pipelines_dir=tmp_pipelines)
        assert r.returncode == 0
        with open(path) as f:
            data = json.load(f)
        assert len(data["batch"]) == 1
        assert data["batch"][0]["issue"] == 42

    def test_transition(self, tmp_pipelines, sample_pipeline):
        sample_pipeline["state"] = "IDLE"
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)

        r = self._run("transition", "--project", "test-pj", "--to", "INITIALIZE",
                       pipelines_dir=tmp_pipelines)
        assert r.returncode == 0
        with open(path) as f:
            assert json.load(f)["state"] == "INITIALIZE"

    def test_review(self, tmp_pipelines, sample_pipeline):
        sample_pipeline["state"] = "DESIGN_REVIEW"
        sample_pipeline["batch"] = [
            {"issue": 10, "title": "t", "design_reviews": {}, "code_reviews": {},
             "commit": None, "cc_session_id": None, "added_at": ""}
        ]
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)

        r = self._run("review", "--project", "test-pj", "--issue", "10",
                       "--reviewer", "pascal", "--verdict", "APPROVE", "--summary", "LGTM",
                       pipelines_dir=tmp_pipelines)
        assert r.returncode == 0
        with open(path) as f:
            data = json.load(f)
        assert "pascal" in data["batch"][0]["design_reviews"]

    def test_commit(self, tmp_pipelines, sample_pipeline):
        sample_pipeline["batch"] = [
            {"issue": 10, "title": "t", "commit": None, "cc_session_id": None,
             "design_reviews": {}, "code_reviews": {}, "added_at": ""}
        ]
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)

        r = self._run("commit", "--project", "test-pj", "--issue", "10", "--hash", "abc123",
                       pipelines_dir=tmp_pipelines)
        assert r.returncode == 0
        with open(path) as f:
            assert json.load(f)["batch"][0]["commit"] == "abc123"

    def test_plan_done(self, tmp_pipelines, sample_pipeline):
        sample_pipeline["state"] = "DESIGN_PLAN"
        sample_pipeline["batch"] = [
            {"issue": 10, "title": "t", "commit": None, "cc_session_id": None,
             "design_reviews": {}, "code_reviews": {}, "added_at": ""}
        ]
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)

        r = self._run("plan-done", "--project", "test-pj", "--issue", "10",
                       pipelines_dir=tmp_pipelines)
        assert r.returncode == 0
        with open(path) as f:
            assert json.load(f)["batch"][0]["design_ready"] is True

    def test_revise(self, tmp_pipelines, sample_pipeline):
        sample_pipeline["state"] = "DESIGN_REVISE"
        sample_pipeline["batch"] = [
            {"issue": 10, "title": "t", "commit": None, "cc_session_id": None,
             "design_reviews": {}, "code_reviews": {}, "added_at": ""}
        ]
        path = tmp_pipelines / "test-pj.json"
        write_pipeline(path, sample_pipeline)

        r = self._run("design-revise", "--project", "test-pj", "--issue", "10",
                       pipelines_dir=tmp_pipelines)
        assert r.returncode == 0
        with open(path) as f:
            assert json.load(f)["batch"][0]["design_revised"] is True
