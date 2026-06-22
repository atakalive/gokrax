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
import math
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


class ContextSizeReader(Protocol):
    """任意拡張インターフェース: 現在の入力コンテキストサイズ (トークン) を返せる reader。

    TranscriptReader の必須メンバーではない。実装している reader だけが進捗通知に
    ctx を出せる。呼び出し側は getattr で存在確認するため、未実装 backend を登録しても
    型・実行とも安全（その backend では ctx 表示がスキップされるだけ）。
    """

    def read_context_size(self, path: Path) -> int | None:
        """入力コンテキスト総トークン数を返す。取得不能なら None。

        None のとき表示をスキップするか直前値へフォールバックするかは呼び出し側が決める。
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

    _CTX_TAIL_BYTES = 2_097_152  # tail read で読む末尾バイト数 (2 MiB)

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

    def read_context_size(self, path: Path) -> int | None:
        """最新 assistant エントリの message.usage から入力コンテキスト総トークン数を返す。

        = input_tokens + cache_read_input_tokens + cache_creation_input_tokens
        (3 キーは相互排他なので二重カウントにならない。output_tokens は含めない)。

        効率のため count_tool_calls の offset 増分読みとは異なり、ファイル末尾
        _CTX_TAIL_BYTES (2 MiB) だけを seek して読み、新しい行から走査する
        (tail-seek の byte 境界最適化はこれで十分。whole-file read はしない)。
        有効なトークンキーを 1 つ以上持つ usage の合計を返す。どのエントリにも
        有効キーが無い / 読めない / OSError は None。本メソッドは例外を投げない。
        """
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - self._CTX_TAIL_BYTES))
                raw_bytes = f.read()
        except OSError:
            return None
        # tail 読みの先頭要素は途中で切れた部分行になりうるが、reverse 走査では最後に
        # 評価され、json.loads 失敗で安全にスキップされる。
        for raw in reversed(raw_bytes.split(b"\n")):
            if not raw.strip():
                continue
            try:
                entry = json.loads(raw)
            except Exception:
                continue
            if not isinstance(entry, dict) or entry.get("type") != "assistant":
                continue
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            total = 0
            found = False
            for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                v = usage.get(key)
                # json.loads は NaN/Infinity と任意精度 int を生む。int はそのまま採用（finite）、
                # float のみ math.isfinite で篩う。int に math.isfinite を使うと巨大 int で
                # OverflowError になり「例外を投げない」契約を壊すため、型で分岐する。
                if isinstance(v, bool):
                    continue
                if isinstance(v, int):
                    total += v
                    found = True
                elif isinstance(v, float) and math.isfinite(v):
                    total += int(v)
                    found = True
            if found:
                return total
            # usage はあるが有効なトークンキーが 1 つも無い → このエントリは捨て、より古い行を試す
            # (~0 tok を「取得失敗」と区別がつかない形で出さないため)。
        return None


register("cc", ClaudeJsonlReader(config.CC_SESSIONS_DIR))
register("cci", ClaudeJsonlReader(config.CCI_SESSIONS_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LAST_MIN_WINDOW = 60.0  # trailing window (seconds) for the "last min" tool-call count

_PROGRESS_KEYS = (
    "progress_phase", "progress_transcript", "progress_offset", "progress_count",
    "progress_started_ts", "progress_msg_id", "progress_samples",
    "progress_ctx_tokens",
)


def _fmt_elapsed(seconds: float) -> str:
    """経過秒を英語の `Hh Mm Ss` / `Xm Ys` 形式に整形する。"""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"


def format_ctx_tokens(tokens: int) -> str:
    """トークン数を人間可読な近似文字列にする (取得は厳密、表示はこの近似)。

    例: 12345 -> '~12.3K tok', 366000 -> '~366K tok', 0 -> '~0 tok'。
    K/M 区間では mantissa が 100 未満なら小数 1 桁、100 以上は整数。丸めで桁が
    繰り上がって単位境界を跨ぐケース (999_500 以上 -> '~1.0M tok'、99_950 以上 ->
    '~100K tok') も連続になるよう、丸め後に単位/小数桁を再判定する。
    """
    n = max(0, int(tokens))
    if n < 1000:
        return f"~{n} tok"
    try:
        if n >= 1_000_000:
            value, suffix = n / 1_000_000.0, "M"
        else:
            value, suffix = n / 1000.0, "K"
        text = f"{value:.1f}" if value < 100 else f"{value:.0f}"
        # 丸めで mantissa が 1000 に達したら上位単位へ繰り上げ (K->M。M は最上位)。
        if float(text) >= 1000 and suffix == "K":
            value, suffix = n / 1_000_000.0, "M"
            text = f"{value:.1f}" if value < 100 else f"{value:.0f}"
        # mantissa<100 だが丸めで 100 に達したら整数表示へ統一 (99.95K -> 100K)。
        if value < 100 and float(text) >= 100:
            text = f"{value:.0f}"
        return f"~{text}{suffix} tok"
    except OverflowError:
        # 壊れた transcript 由来の float 変換不能な巨大整数はそのまま表示（表示パスをクラッシュさせない）。
        return f"~{n} tok"


def _last_min_count(samples: list, count: int, now: float, started_ts: float) -> int:
    """Tool calls in the trailing 60s, derived from prior tick snapshots.

    `samples` is the persisted ring buffer, ordered oldest→newest (the invariant
    maintained by `_append_and_prune`). We pick the latest snapshot at/before the
    window edge as the baseline and return `count - baseline`.
    """
    window_start = now - _LAST_MIN_WINDOW
    base: int | None = None
    for ts, c in samples:               # sorted ascending → last match = newest pre-window snapshot
        if ts <= window_start:
            base = int(c)
    if base is None:
        if started_ts >= window_start:  # phase began inside the window → all calls are recent
            return count
        base = int(samples[0][1]) if samples else 0  # first-tick / mid-restart fallback
    return max(0, count - base)


def _append_and_prune(samples: list, now: float, count: int) -> list:
    """Append the current snapshot; keep the in-window samples plus one anchor just older.

    The anchor is the newest snapshot strictly older than the window edge — `_last_min_count`
    needs it as the `count_at(now - 60s)` baseline. Older snapshots beyond that single anchor
    are dropped. Returns a fresh list of `[ts, count]` pairs (no aliasing of the input),
    ordered oldest→newest, bounded to roughly `60s / interval + 2` entries (~4–7 in practice).
    """
    window_start = now - _LAST_MIN_WINDOW
    kept = [list(s) for s in samples if s[0] >= window_start]
    older = [s for s in samples if s[0] < window_start]
    out = ([list(max(older, key=lambda s: s[0]))] if older else []) + kept
    out.append([now, count])
    return out


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
        started_ts = data.get("progress_started_ts")
        suffix = ""
        if started_ts is not None:
            elapsed = max(now - float(started_ts), 1.0)
            avg = count / (elapsed / 60.0)
            suffix = f" · avg {avg:.1f}/min · ⏱ {_fmt_elapsed(elapsed)}"
        if state == "BLOCKED":
            text = f"[{pj}] ⏹ {phase} ended ({data.get('blocked_reason') or 'blocked'}) — {count} tool calls{suffix}"
        elif state in ("IDLE", "DONE"):
            text = f"[{pj}] ⏹ {phase} ended — {count} tool calls{suffix}"
        else:
            text = f"[{pj}] ✅ {phase} complete — {count} tool calls{suffix}"
        ctx_tokens = data.get("progress_ctx_tokens")
        if isinstance(ctx_tokens, int) and not isinstance(ctx_tokens, bool):
            text += f" · ctx {format_ctx_tokens(ctx_tokens)}"
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
        samples = []
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
        samples = data.get("progress_samples") or []
        msg_id = data.get("progress_msg_id")

    # 6. 指標算出（ガード順序重要）。
    elapsed = max(now - started_ts, 1.0)
    avg = count / (elapsed / 60.0)
    last_min = _last_min_count(samples, count, now, started_ts)
    elapsed_str = _fmt_elapsed(elapsed)

    text = (
        f"[{pj}] 🔧 {state} in progress — {count} tool calls · "
        f"avg {avg:.1f}/min · last min {last_min} · ⏱ {elapsed_str}"
    )

    # 6b. context size（任意拡張）。supports_ctx=False の未対応 backend は ctx を一切出さない不変条件。
    read_ctx = getattr(reader, "read_context_size", None)
    supports_ctx = callable(read_ctx)
    raw_ctx = read_ctx(pt) if supports_ctx else None
    # 型検証: int(bool 除く) か None のみ正常。reader が非 int を返したら誤実装 → invalid_ctx。
    valid_ctx = isinstance(raw_ctx, int) and not isinstance(raw_ctx, bool)
    ctx_tokens = raw_ctx if valid_ctx else None
    invalid_ctx = supports_ctx and raw_ctx is not None and not valid_ctx
    # 表示用フォールバック: 正常な ctx 対応 reader が今回 None(取得失敗) かつ非 fresh のときのみ直前値を使う。
    # 未対応(not supports_ctx)・誤実装(invalid_ctx) では last-known を出さない（整形関数へ非 int を渡さない）。
    display_ctx = ctx_tokens
    if supports_ctx and not invalid_ctx and display_ctx is None and not fresh:
        prev = data.get("progress_ctx_tokens")
        if isinstance(prev, int) and not isinstance(prev, bool):
            display_ctx = prev
    if display_ctx is not None:
        text += f" · ctx {format_ctx_tokens(display_ctx)}"

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
    final_samples = _append_and_prune(samples, now, final_count)
    final_ctx_tokens = ctx_tokens

    def _cb(d: dict) -> None:
        d["progress_phase"] = state
        d["progress_transcript"] = ps
        d["progress_offset"] = final_offset
        d["progress_count"] = final_count
        d["progress_started_ts"] = final_started
        d["progress_samples"] = final_samples
        if final_msg_id is not None:
            d["progress_msg_id"] = final_msg_id
        else:
            d.pop("progress_msg_id", None)
        if final_ctx_tokens is not None:
            d["progress_ctx_tokens"] = final_ctx_tokens
        elif fresh or not supports_ctx or invalid_ctx:
            # 新フェーズ初回の取得失敗、ctx 非対応 reader、または誤実装(非int) → 残骸を残さない。
            d.pop("progress_ctx_tokens", None)
        # supports_ctx かつ valid かつ not fresh かつ None: 最後に取得できた値を保持（finalize / 次 tick フォールバック用）。

    update_pipeline(path, _cb)
