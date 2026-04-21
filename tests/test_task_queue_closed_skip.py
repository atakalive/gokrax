"""tests/test_task_queue_closed_skip.py — pop_next_queue_entry の closed skip テスト"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _write_pipeline(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _make_idle_pipeline(project: str = "proj1") -> dict:
    return {
        "project": project, "state": "IDLE",
        "enabled": False, "batch": [], "history": [],
        "gitlab": f"testns/{project}", "repo_path": f"/tmp/repo/{project}",
        "implementer": "implementer1",
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    import config as _config
    import pipeline_io as _pio
    pipelines_dir = tmp_path / "pipelines"
    pipelines_dir.mkdir()
    monkeypatch.setattr(_config, "PIPELINES_DIR", pipelines_dir)
    monkeypatch.setattr(_pio, "PIPELINES_DIR", pipelines_dir)
    monkeypatch.setattr(_config, "DISCORD_CHANNEL", "test-channel")
    return tmp_path, pipelines_dir


@pytest.fixture
def post_spy(monkeypatch):
    calls: list[tuple[str, str]] = []

    def _spy(channel: str, content: str, retries: int = 3):
        calls.append((channel, content))
        from notify import DiscordPostResult
        return DiscordPostResult("mock-id")

    monkeypatch.setattr("notify.post_discord", _spy)
    return calls


def _patch_fetch(monkeypatch, fn):
    monkeypatch.setattr("engine.glab.fetch_issue_state", fn)


def test_t1_allow_closed(env, post_spy, monkeypatch):
    tmp, pdir = env
    _write_pipeline(pdir / "proj1.json", _make_idle_pipeline("proj1"))
    q = tmp / "q.txt"
    q.write_text("proj1 1,2 allow-closed\n")

    calls = []
    _patch_fetch(monkeypatch, lambda n, g: calls.append(n) or "closed")

    from task_queue import pop_next_queue_entry
    r = pop_next_queue_entry(q)
    assert r is not None and r["project"] == "proj1"
    assert calls == []
    assert post_spy == []


def test_t2_all_opened(env, post_spy, monkeypatch):
    tmp, pdir = env
    _write_pipeline(pdir / "proj1.json", _make_idle_pipeline("proj1"))
    q = tmp / "q.txt"
    q.write_text("proj1 1,2\n")
    _patch_fetch(monkeypatch, lambda n, g: "opened")

    from task_queue import pop_next_queue_entry
    r = pop_next_queue_entry(q)
    assert r is not None
    assert post_spy == []


def test_t3_partial_closed_next(env, post_spy, monkeypatch):
    tmp, pdir = env
    _write_pipeline(pdir / "proj1.json", _make_idle_pipeline("proj1"))
    _write_pipeline(pdir / "proj2.json", _make_idle_pipeline("proj2"))
    q = tmp / "q.txt"
    q.write_text("proj1 1,2,3\nproj2 5\n")

    def fake(n, g):
        return "closed" if n == 2 else "opened"
    _patch_fetch(monkeypatch, fake)

    from task_queue import pop_next_queue_entry
    r = pop_next_queue_entry(q)
    assert r is not None and r["project"] == "proj2"
    content = q.read_text()
    assert content.startswith("# done: proj1 1,2,3")
    assert len(post_spy) == 1
    assert "#2" in post_spy[0][1] and "closed" in post_spy[0][1]


def test_t4_issues_all(env, post_spy, monkeypatch):
    tmp, pdir = env
    _write_pipeline(pdir / "proj1.json", _make_idle_pipeline("proj1"))
    q = tmp / "q.txt"
    q.write_text("proj1 all\n")

    calls = []
    _patch_fetch(monkeypatch, lambda n, g: calls.append(n) or "closed")

    from task_queue import pop_next_queue_entry
    r = pop_next_queue_entry(q)
    assert r is not None
    assert calls == []
    assert post_spy == []


def test_t5_unverified_notify(env, post_spy, monkeypatch):
    tmp, pdir = env
    _write_pipeline(pdir / "proj1.json", _make_idle_pipeline("proj1"))
    q = tmp / "q.txt"
    q.write_text("proj1 1,2\n")
    _patch_fetch(monkeypatch, lambda n, g: None)

    from task_queue import pop_next_queue_entry
    r = pop_next_queue_entry(q)
    assert r is not None
    assert len(post_spy) == 1
    assert "unverified" in post_spy[0][1]


def test_t6_all_closed(env, post_spy, monkeypatch):
    tmp, pdir = env
    _write_pipeline(pdir / "proj1.json", _make_idle_pipeline("proj1"))
    _write_pipeline(pdir / "proj2.json", _make_idle_pipeline("proj2"))
    q = tmp / "q.txt"
    q.write_text("proj1 1\nproj2 2\n")
    _patch_fetch(monkeypatch, lambda n, g: "closed")

    from task_queue import pop_next_queue_entry
    r = pop_next_queue_entry(q)
    assert r is None
    content = q.read_text()
    assert content.count("# done:") == 2
    assert len(post_spy) == 2


def test_t7_lock_released_before_notify(env, monkeypatch):
    tmp, pdir = env
    _write_pipeline(pdir / "proj1.json", _make_idle_pipeline("proj1"))
    q = tmp / "q.txt"
    q.write_text("proj1 1\n")
    _patch_fetch(monkeypatch, lambda n, g: "closed")

    events: list[str] = []
    import fcntl as _fcntl
    orig_flock = _fcntl.flock

    def spy_flock(fd, op):
        if op == _fcntl.LOCK_UN:
            events.append("UNLOCK")
        return orig_flock(fd, op)
    monkeypatch.setattr("task_queue.fcntl.flock", spy_flock)

    def spy_post(ch, content, retries=3):
        events.append("POST")
        from notify import DiscordPostResult
        return DiscordPostResult("mock-id")
    monkeypatch.setattr("notify.post_discord", spy_post)

    from task_queue import pop_next_queue_entry
    pop_next_queue_entry(q)
    last_unlock = max(i for i, e in enumerate(events) if e == "UNLOCK")
    first_post = events.index("POST")
    assert last_unlock < first_post


def test_t8_closed_and_unverified_mixed(env, post_spy, monkeypatch):
    tmp, pdir = env
    _write_pipeline(pdir / "proj1.json", _make_idle_pipeline("proj1"))
    q = tmp / "q.txt"
    q.write_text("proj1 1,2\n")

    def fake(n, g):
        return "closed" if n == 1 else None
    _patch_fetch(monkeypatch, fake)

    from task_queue import pop_next_queue_entry
    r = pop_next_queue_entry(q)
    assert r is None
    assert len(post_spy) == 1
    assert "closed" in post_spy[0][1]
    assert "unverified" not in post_spy[0][1]


def test_t9_discord_channel_empty(env, post_spy, monkeypatch):
    tmp, pdir = env
    import config as _config
    monkeypatch.setattr(_config, "DISCORD_CHANNEL", "")
    _write_pipeline(pdir / "proj1.json", _make_idle_pipeline("proj1"))
    q = tmp / "q.txt"
    q.write_text("proj1 1\n")
    _patch_fetch(monkeypatch, lambda n, g: "closed")

    from task_queue import pop_next_queue_entry
    pop_next_queue_entry(q)
    assert post_spy == []


def test_t10_closed_nonidle_ok(env, post_spy, monkeypatch):
    tmp, pdir = env
    _write_pipeline(pdir / "proj1.json", _make_idle_pipeline("proj1"))
    p2 = _make_idle_pipeline("proj2")
    p2["state"] = "DESIGN_PLAN"
    _write_pipeline(pdir / "proj2.json", p2)
    _write_pipeline(pdir / "proj3.json", _make_idle_pipeline("proj3"))
    q = tmp / "q.txt"
    q.write_text("proj1 1\nproj2 2\nproj3 3\n")

    def fake(n, g):
        return "closed" if n == 1 else "opened"
    _patch_fetch(monkeypatch, fake)

    from task_queue import pop_next_queue_entry
    r = pop_next_queue_entry(q)
    assert r is not None and r["project"] == "proj3"
    assert len(post_spy) == 1


def test_t11_crash_safety_file_unchanged_in_phase_b(env, post_spy, monkeypatch):
    tmp, pdir = env
    _write_pipeline(pdir / "proj1.json", _make_idle_pipeline("proj1"))
    q = tmp / "q.txt"
    q.write_text("proj1 1,2\n")

    snapshots: list[str] = []

    def fake(n, g):
        snapshots.append(q.read_text())
        return "opened"
    _patch_fetch(monkeypatch, fake)

    from task_queue import pop_next_queue_entry
    pop_next_queue_entry(q)
    for snap in snapshots:
        assert "# done:" not in snap


def test_t11b_phase_a_readonly_no_overwrite(env, post_spy, monkeypatch):
    """P2-B: Phase A が読み取り専用であることの mutation 試験。
    Phase B 侵入時に外部がファイル内容を mutate しても、Phase A 側は
    ファイルに書き戻さない（Phase A は "r" open なので構造上不可能だが、
    実装変更で writable open になる退行を検出する）。
    """
    tmp, pdir = env
    _write_pipeline(pdir / "proj1.json", _make_idle_pipeline("proj1"))
    q = tmp / "q.txt"
    q.write_text("proj1 1\n")

    def fake(n, g):
        # Phase B 侵入時点で外部がファイルを完全に差し替える
        q.write_text("proj1 999 mutated_by_external\n")
        return "opened"
    _patch_fetch(monkeypatch, fake)

    from task_queue import pop_next_queue_entry
    pop_next_queue_entry(q)
    # Phase A の結果が file に逆流していないこと（Phase C は content-based
    # revalidation で original_line を見つけられず False を返すので書き戻しなし）
    content = q.read_text()
    assert "proj1 999 mutated_by_external" in content
    # Phase A が読んだ "proj1 1" を上書き保存していたら mutate 行が消えるはず
    # → 残っているので Phase A は書き戻していない


def test_t12_no_closed_nums_no_notify(env, post_spy, monkeypatch):
    tmp, pdir = env
    _write_pipeline(pdir / "proj1.json", _make_idle_pipeline("proj1"))
    q = tmp / "q.txt"
    q.write_text("proj1 1,2\n")
    _patch_fetch(monkeypatch, lambda n, g: "opened")

    from task_queue import pop_next_queue_entry
    r = pop_next_queue_entry(q)
    assert r is not None
    assert post_spy == []


def test_t13_concurrent_done_skip(env, post_spy, monkeypatch):
    tmp, pdir = env
    _write_pipeline(pdir / "proj1.json", _make_idle_pipeline("proj1"))
    _write_pipeline(pdir / "proj2.json", _make_idle_pipeline("proj2"))
    q = tmp / "q.txt"
    q.write_text("proj1 1\nproj2 2\n")

    # Fake fetch that, on first call, simulates another process having already
    # done-marked the first line mid-Phase-B.
    state = {"hit": False}

    def fake(n, g):
        if not state["hit"]:
            state["hit"] = True
            lines = q.read_text().splitlines(keepends=True)
            for i, ln in enumerate(lines):
                if ln.startswith("proj1 1"):
                    lines[i] = f"# done: {ln}"
                    break
            q.write_text("".join(lines))
        return "opened"

    _patch_fetch(monkeypatch, fake)

    from task_queue import pop_next_queue_entry
    r = pop_next_queue_entry(q)
    # First candidate's commit fails (already done), falls through to proj2.
    assert r is not None and r["project"] == "proj2"
