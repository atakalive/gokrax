"""tests/test_path_traversal.py — get_path のパストラバーサル防御テスト"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestPathTraversal:

    def test_dotdot_traversal(self, tmp_pipelines):
        from pipeline_io import get_path
        with pytest.raises(SystemExit, match="Invalid project name"):
            get_path("../../../tmp/evil")

    def test_spaces(self, tmp_pipelines):
        from pipeline_io import get_path
        with pytest.raises(SystemExit, match="Invalid project name"):
            get_path("has spaces")

    def test_slash(self, tmp_pipelines):
        from pipeline_io import get_path
        with pytest.raises(SystemExit, match="Invalid project name"):
            get_path("a/b")

    def test_normal_name(self, tmp_pipelines):
        from pipeline_io import get_path
        path = get_path("normal-name")
        assert path.name == "normal-name.json"
        assert str(tmp_pipelines.resolve()) in str(path)

    def test_underscore_digits(self, tmp_pipelines):
        from pipeline_io import get_path
        path = get_path("Under_Score123")
        assert path.name == "Under_Score123.json"

    def test_empty_string(self, tmp_pipelines):
        from pipeline_io import get_path
        with pytest.raises(SystemExit, match="Invalid project name"):
            get_path("")
