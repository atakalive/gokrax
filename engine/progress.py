"""engine/progress.py — DESIGN_PLAN / REVISE フェーズの Discord 進捗通知 (#382)。

実装者エージェントが transcript に書く tool_use ブロックを数え、フェーズ開始基準
時刻以降の累計 tool call 数 + 速度を 1 つの Discord メッセージに出し、tick 毎に
同一メッセージを上書き更新する。

設計方針: backend / cci_runner / dispatch 層には一切手を入れず、計数・通知を
すべて watchdog 側 (本モジュール) に集約する。tool call の数え方だけを
`TranscriptReader` 抽象として backend 別に差し替え可能にする。今回は cc / cci 用の
`ClaudeJsonlReader` のみ登録する (pi/gemini/kimi/agy は将来 reader 追加で拡張)。
"""

from __future__ import annotations

import glob
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Protocol

import config
import notify
from config.states import PROGRESS_TRACKED_STATES
from engine.backend import resolve_backend
from engine.fsm import _get_state_entered_at
from pipeline_io import update_pipeline

logger = logging.getLogger("gokrax.progress")

_UUID_RE_LEN = 36  # canonical UUID string length (defensive sanity check)


# ---------------------------------------------------------------------------
# (a) TranscriptReader 抽象 + registry
# ---------------------------------------------------------------------------

class TranscriptReader(Protocol):
    def resolve(self, agent_id: str, repo_path: str) -> Path | None:
        """この backend のアクティブな transcript を特定（無ければ None）。"""
        ...

    def count_tool_calls(self, path: Path, offset: int,
                         since_epoch: float | None) -> tuple[int, int] | None:
        """offset 以降の完全行を読み tool 呼び出し数と新 offset を返す。

        since_epoch が None でなければ、各エントリの timestamp(epoch) >= since_epoch
        のものだけ数える。読み取り失敗(OSError) 時は None。
        """
        ...


_READERS: dict[str, TranscriptReader] = {}


def register(backend: str, reader: TranscriptReader) -> None:
    _READERS[backend] = reader


def get_reader(backend: str) -> TranscriptReader | None:
    return _READERS.get(backend)


def _looks_like_uuid(s: str) -> bool:
    """UUID 形式の緩いチェック（ハイフン込み 36 文字・16進+ハイフンのみ）。"""
    if len(s) != _UUID_RE_LEN:
        return False
    return all(c in "0123456789abcdefABCDEF-" for c in s)


class ClaudeJsonlReader:
    """cc / cci backend 用 claude JSONL transcript reader。

    各 backend の session_id ファイル位置 (CC_SESSIONS_DIR / CCI_SESSIONS_DIR) だけが
    異なるため、sessions_dir をコンストラクタで受ける。
    """

    def __init__(self, sessions_dir: Path) -> None:
        self.sessions_dir = sessions_dir

    def resolve(self, agent_id: str, repo_path: str) -> Path | None:
        # 1. session_id を読む。
        sid_path = self.sessions_dir / agent_id / "session_id"
        try:
            sid = sid_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not sid or not _looks_like_uuid(sid):
            return None

        # 2. repo_path 非空なら予測パスを優先。
        if repo_path:
            project_key = str(Path(repo_path).resolve()).replace("/", "-")
            predicted = Path.home() / ".claude" / "projects" / project_key / f"{sid}.jsonl"
            if predicted.exists():
                return predicted

        # 3. 予測パス不在 / repo_path 空なら glob フォールバック（mtime 最新）。
        pattern = str(Path.home() / ".claude" / "projects" / "*" / f"{sid}.jsonl")
        candidates = glob.glob(pattern)
        if not candidates:
            return None
        if len(candidates) == 1:
            chosen = Path(candidates[0])
        else:
            chosen = Path(max(candidates, key=os.path.getmtime))
            logger.debug("progress: multiple transcript candidates for %s, picked %s by mtime",
                         agent_id, chosen)
        if repo_path:
            logger.debug("progress: predicted path missing for %s, fell back to glob -> %s",
                         agent_id, chosen)
        return chosen

    def count_tool_calls(self, path: Path, offset: int,
                         since_epoch: float | None) -> tuple[int, int] | None:
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                chunk = f.read()
        except OSError:
            return None
        if not chunk:
            return 0, offset
        nl = chunk.rfind(b"\n")
        if nl < 0:
            # 完全行が無い（末尾部分行のみ）→ offset 据え置き。
            return 0, offset
        complete = chunk[: nl + 1]
        new_offset = offset + nl + 1

        count = 0
        for raw in complete.split(b"\n"):
            if not raw.strip():
                continue
            try:
                entry = json.loads(raw)
            except Exception:
                continue
            if not isinstance(entry, dict) or entry.get("type") != "assistant":
                continue
            if since_epoch is not None:
                ts = entry.get("timestamp")
                if not isinstance(ts, str):
                    continue
                try:
                    ets = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                except (ValueError, TypeError):
                    continue
                if ets < since_epoch:
                    continue
            message = entry.get("message") or {}
            content = message.get("content") or []
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    count += 1
        return count, new_offset


register("cc", ClaudeJsonlReader(config.CC_SESSIONS_DIR))
register("cci", ClaudeJsonlReader(config.CCI_SESSIONS_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PROGRESS_KEYS = (
    "progress_phase", "progress_transcript", "progress_offset", "progress_count",
    "progress_started_ts", "progress_msg_id", "progress_prev_count", "progress_prev_ts",
)


def _fmt_elapsed(seconds: float) -> str:
    """経過秒を英語の `Hh Mm Ss` / `Xm Ys` 形式に整形する。"""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"


def _clear_progress(path: Path) -> None:
    def _cb(data: dict) -> None:
        for k in _PROGRESS_KEYS:
            data.pop(k, None)
    update_pipeline(path, _cb)


# ---------------------------------------------------------------------------
# (b) オーケストレーション
# ---------------------------------------------------------------------------

def update_phase_progress(path: Path, state: str, data: dict) -> None:
    """対象フェーズの進捗を Discord に 1 メッセージで出し、tick 毎に上書き更新する。

    backend 非依存。tool call の数え方は TranscriptReader registry が担う。
    """
    now = time.time()
    pj = data.get("project", path.stem)

    # マスタースイッチ（#382）。settings.py で PROGRESS_NOTIFY=True にすると有効化。
    if not config.PROGRESS_NOTIFY:
        return

    # Discord 可用性は DISCORD_CHANNEL の有無のみで判定（token / DRY_RUN は notify 側に委ねる）。
    if not config.DISCORD_CHANNEL:
        return

    # === 非対象フェーズ = 最終化（anchor 有無で判定。msg_id 有無では判定しない）===
    if state not in PROGRESS_TRACKED_STATES:
        if not data.get("progress_phase"):
            return
        phase = data.get("progress_phase", "phase")
        count = int(data.get("progress_count", 0))
        if state == "BLOCKED":
            text = f"[{pj}] ⏹ {phase} ended ({data.get('blocked_reason') or 'blocked'}) — {count} tool calls"
        elif state in ("IDLE", "DONE"):
            text = f"[{pj}] ⏹ {phase} ended — {count} tool calls"
        else:
            text = f"[{pj}] ✅ {phase} complete — {count} tool calls"
        msg_id = data.get("progress_msg_id")
        if msg_id:
            notify.edit_discord_message(config.DISCORD_CHANNEL, msg_id, text)
        _clear_progress(path)
        return

    # === 対象フェーズ ===
    # 0. enabled 尊重（BLOCKED 等で無効化された pipeline では更新を止める）。
    if not data.get("enabled", False):
        return

    # 1. implementer 解決（クラッシュ回避）。
    implementer = data.get("implementer")
    if not implementer:
        if not config.IMPLEMENTERS:
            return
        implementer = config.IMPLEMENTERS[0]

    # 2. backend 解決 + reader 取得。
    try:
        backend = resolve_backend(implementer)
    except ValueError:
        return
    reader = get_reader(backend)
    if reader is None:
        return

    # 3. transcript 解決。
    pt = reader.resolve(implementer, data.get("repo_path", ""))
    if pt is None:
        return
    ps = str(pt)

    # 4. anchor 鮮度判定（msg_id とは独立）。
    entered = _get_state_entered_at(data, state)
    cur_start = entered.timestamp() if entered else None
    fresh = (
        data.get("progress_phase") != state
        or data.get("progress_transcript") != ps
        or ("progress_started_ts" not in data)
    )
    if cur_start is not None and abs(float(data.get("progress_started_ts", 0)) - cur_start) > 1.0:
        fresh = True
    try:
        size = pt.stat().st_size
    except OSError:
        return
    if (not fresh) and size < int(data.get("progress_offset", 0)):
        fresh = True

    # 5. 計数。
    if fresh:
        if entered is not None:
            started_ts = cur_start
            r = reader.count_tool_calls(pt, 0, since_epoch=started_ts)
            if r is None:
                return
            count, offset = r
        else:
            # history 欠損 / MAX_HISTORY 切り詰め → EOF baseline（既存内容は数えない）。
            started_ts = now
            offset = size
            count = 0
        prev_count, prev_ts = count, now
        same_cycle = (data.get("progress_phase") == state) and (
            cur_start is None
            or abs(float(data.get("progress_started_ts", 0)) - cur_start) <= 1.0
        )
        msg_id = data.get("progress_msg_id") if same_cycle else None
    else:
        offset = int(data.get("progress_offset", 0))
        count = int(data.get("progress_count", 0))
        started_ts = float(data.get("progress_started_ts", now))
        r = reader.count_tool_calls(pt, offset, since_epoch=None)
        if r is None:
            return
        added, offset = r
        count += added
        prev_count = int(data.get("progress_prev_count", count))
        prev_ts = float(data.get("progress_prev_ts", now))
        msg_id = data.get("progress_msg_id")

    # 6. 指標算出（ガード順序重要）。
    elapsed = max(now - started_ts, 1.0)
    avg = count / (elapsed / 60.0)
    dt = now - prev_ts
    now_rate = avg if dt <= 0 else max(0.0, (count - prev_count) / (dt / 60.0))
    elapsed_str = _fmt_elapsed(elapsed)

    text = (
        f"[{pj}] 🔧 {state} in progress — {count} tool calls · "
        f"avg {avg:.1f}/min · now {now_rate:.1f}/min · ⏱ {elapsed_str}"
    )

    # 7. 投稿 / 編集。
    if not msg_id:
        res = notify.post_discord(config.DISCORD_CHANNEL, text)
        msg_id = res.message_id
    else:
        status = notify.edit_discord_message(config.DISCORD_CHANNEL, msg_id, text)
        if status == "deleted":
            msg_id = None

    # 8. 永続化（1 回の update_pipeline でまとめて）。
    final_offset = offset
    final_count = count
    final_started = started_ts
    final_msg_id = msg_id

    def _cb(d: dict) -> None:
        d["progress_phase"] = state
        d["progress_transcript"] = ps
        d["progress_offset"] = final_offset
        d["progress_count"] = final_count
        d["progress_started_ts"] = final_started
        d["progress_prev_count"] = final_count
        d["progress_prev_ts"] = now
        if final_msg_id is not None:
            d["progress_msg_id"] = final_msg_id
        else:
            d.pop("progress_msg_id", None)

    update_pipeline(path, _cb)
