"""Tests for _start_cc empty repo_path guard (Issue #267)."""

import json

import pytest

from engine.cc import _start_cc


class TestStartCcGuard:
    def test_empty_repo_path_raises_value_error(self, tmp_path):
        pp = tmp_path / "test.json"
        pp.write_text(json.dumps({"project": "test", "state": "IMPLEMENTATION"}))
        with pytest.raises(ValueError, match="repo_path is empty"):
            _start_cc(project="test", batch=[], gitlab="ns/test", repo_path="", pipeline_path=pp)
