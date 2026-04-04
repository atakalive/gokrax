"""spec_review.py — spec mode レビュー結果パース・判定・統合"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

import yaml  # PyYAML (already in deps)

from config import REVIEW_MODES
from engine.fsm import get_min_reviews

# ---------------------------------------------------------------------------
# 1-A. 定数: VERDICT_ALIASES, SEVERITY_ALIASES, SEVERITY_ORDER
# ---------------------------------------------------------------------------

VERDICT_ALIASES: dict[str, str] = {
    "approve": "APPROVE",
    "p0": "P0",
    "reject": "P0",
    "p1": "P1",
}

SEVERITY_ALIASES: dict[str, str] = {
    "critical": "critical",
    "major": "major",
    "minor": "minor",
    "suggestion": "suggestion",
}

# 統合レポートのソート用（低index = 高重篤度）
SEVERITY_ORDER: dict[str, int] = {
    "critical": 0, "major": 1, "minor": 2, "suggestion": 3,
}

# ---------------------------------------------------------------------------
# 1-B. データクラス（§5.5）
# ---------------------------------------------------------------------------


@dataclass
class SpecReviewItem:
    id: str                    # "C-1" (reviewer-local)
    severity: str              # "critical"|"major"|"minor"|"suggestion"
    section: str               # "§6.2"
    title: str
    description: str
    suggestion: str | None
    reviewer: str              # "pascal"
    normalized_id: str         # "pascal:C-1"


@dataclass
class SpecReviewResult:
    reviewer: str
    verdict: str               # "APPROVE"|"P0"|"P1"|"P2" ("" when parse_success=False)
    items: list[SpecReviewItem]
    raw_text: str
    parse_success: bool


@dataclass
class MergedReviewReport:
    reviews: list[SpecReviewResult]
    all_items: list[SpecReviewItem]   # sorted by severity
    summary: dict[str, int]           # {"critical": n, "major": n, ...}
    highest_verdict: str              # "APPROVE"|"P0"|"P1"|"P2"


# ---------------------------------------------------------------------------
# 1-C. parse_review_yaml
# ---------------------------------------------------------------------------

_YAML_BLOCK_RE = re.compile(
    r"```ya?ml\s*\n(.*?)```",
    re.DOTALL,
)


def _fail(raw_text: str, reviewer: str) -> SpecReviewResult:
    """パース失敗時の共通返却。"""
    return SpecReviewResult(
        reviewer=reviewer, verdict="", items=[],
        raw_text=raw_text, parse_success=False,
    )


def parse_review_yaml(raw_text: str, reviewer: str) -> SpecReviewResult:
    """レビュー応答テキストからYAMLブロックを抽出しパースする。

    - 最初の ```yaml ... ``` ブロックのみ使用
    - verdict/severity は .strip().lower() で正規化してからエイリアス解決
    - verdict/severity の不正値 → parse_success=False, items=[], verdict=""
    - items が None または キー欠落 → 空リストとして扱い parse_success=True
      （APPROVEでitems省略は正常動作。§5.5: verdict有効+items空=有効レビュー）
    - items が非list型（文字列等） → parse_success=False
    - item 必須キー（id, severity）欠落 → parse_success=False
    """
    match = _YAML_BLOCK_RE.search(raw_text)
    if not match:
        return _fail(raw_text, reviewer)

    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return _fail(raw_text, reviewer)

    if not isinstance(data, dict):
        return _fail(raw_text, reviewer)

    # verdict エイリアス解決（strip().lower() で正規化）
    raw_verdict = str(data.get("verdict", "")).strip().lower()
    verdict = VERDICT_ALIASES.get(raw_verdict)
    if verdict is None:
        return _fail(raw_text, reviewer)

    # items ガード: None/非list → 失敗
    raw_items = data.get("items") or []
    if not isinstance(raw_items, list):
        return _fail(raw_text, reviewer)

    # items パース
    items: list[SpecReviewItem] = []
    for item_data in raw_items:
        if not isinstance(item_data, dict):
            return _fail(raw_text, reviewer)

        # 必須キー: id, severity（欠落 → parse失敗）
        if "id" not in item_data or "severity" not in item_data:
            return _fail(raw_text, reviewer)

        raw_sev = str(item_data["severity"]).strip().lower()
        severity = SEVERITY_ALIASES.get(raw_sev)
        if severity is None:
            return _fail(raw_text, reviewer)

        item_id = str(item_data["id"])
        items.append(SpecReviewItem(
            id=item_id,
            severity=severity,
            section=str(item_data.get("section", "")),
            title=str(item_data.get("title", "")),
            description=str(item_data.get("description", "")),
            suggestion=item_data.get("suggestion"),
            reviewer=reviewer,
            normalized_id=f"{reviewer}:{item_id}",
        ))

    return SpecReviewResult(
        reviewer=reviewer, verdict=verdict, items=items,
        raw_text=raw_text, parse_success=True,
    )


# ---------------------------------------------------------------------------
# 1-D. validate_received_entry
# ---------------------------------------------------------------------------

def validate_received_entry(entry: dict) -> bool:
    """current_reviews.entries[reviewer] の不変条件を検査する。

    received 状態で以下が満たされなければ False:
    - verdict ∈ {"APPROVE", "P0", "P1", "P2"} かつ None でない
    - items が list
    - parse_success が True
    """
    if entry.get("verdict") not in ("APPROVE", "P0", "P1", "P2"):
        return False
    if not isinstance(entry.get("items"), list):
        return False
    if entry.get("parse_success") is not True:
        return False
    return True


# ---------------------------------------------------------------------------
# 1-E. should_continue_review
# ---------------------------------------------------------------------------

def should_continue_review(
    spec_config: dict,
    review_mode: str,
    min_reviews_override: int | None = None,
) -> str:
    """SPEC_REVIEW完了後の判定。

    Returns: "revise"|"approved"|"stalled"|"failed"|"paused"

    Raises:
        KeyError: spec_config に rev_index/max_revise_cycles がない場合
            （壊れたデータの早期検出）
    """
    cr = spec_config.get("current_reviews", {})
    reviewer_entries = cr.get("entries", {})

    # received のうち不変条件を満たすもののみ有効（Leibniz P0-4）
    received: dict[str, dict] = {}
    parsed_fail: dict[str, dict] = {}
    for k, v in reviewer_entries.items():
        status = v.get("status")
        if status == "received":
            if validate_received_entry(v):
                received[k] = v
            else:
                parsed_fail[k] = v  # invariant violation -> demote to parse_failed
        elif status == "parse_failed":
            parsed_fail[k] = v
        # timeout は received にも parsed_fail にも含まれない

    if review_mode not in REVIEW_MODES:
        raise ValueError(f"Unknown review_mode: {review_mode!r}")
    min_valid = min_reviews_override if min_reviews_override is not None else get_min_reviews(REVIEW_MODES[review_mode])

    # 1. 全員タイムアウト（received=0, parsed_fail=0）
    if len(received) == 0 and len(parsed_fail) == 0:
        return "failed"

    # 2. 有効レビュー不足
    if len(received) < min_valid:
        if len(parsed_fail) > 0:
            return "paused"   # parse failure present -> human intervention needed
        return "failed"       # timeout only -> recoverable by resend

    # 3. 有効レビューで判定（received >= min_valid: parsed_fail は無視して続行）
    has_p1 = any(
        v.get("verdict") in ("P0", "P1", "P2") for v in received.values()
    )
    if not has_p1:
        return "approved"

    # 4. MAX到達 → stalled（直アクセス: Dijkstra P1-2）
    if spec_config["rev_index"] >= spec_config["max_revise_cycles"]:
        return "stalled"

    return "revise"


# ---------------------------------------------------------------------------
# 1-F. _reset_review_requests
# ---------------------------------------------------------------------------

def _reset_review_requests(spec_config: dict, now: datetime) -> None:
    """SPEC_REVIEWへ遷移する全パスで呼ばれるリセット関数。

    Args:
        spec_config: パイプラインの spec_config。
        now: リセット時刻（§5.4 準拠、将来のログ用に保持）。
    """
    for _reviewer, entry in spec_config.get("review_requests", {}).items():
        entry["status"] = "pending"
        entry["sent_at"] = None
        entry["timeout_at"] = None
        entry["last_nudge_at"] = None
        entry["response"] = None


# ---------------------------------------------------------------------------
# 1-G. merge_reviews
# ---------------------------------------------------------------------------

def merge_reviews(reviews: list[SpecReviewResult]) -> MergedReviewReport:
    """複数レビュー結果を統合し、重篤度順にソートしたレポートを生成する。

    前提条件:
        reviews には parse_success=True の SpecReviewResult のみを渡すこと。
        parse_success=False のものが混入した場合、verdict="" は最低優先度として扱われ、
        items は空なので実害はないが、呼び出し側でフィルタすべき。
    """
    all_items: list[SpecReviewItem] = []
    for r in reviews:
        all_items.extend(r.items)

    # 重篤度順ソート
    all_items.sort(key=lambda x: SEVERITY_ORDER.get(x.severity, 99))

    summary: dict[str, int] = {
        "critical": 0, "major": 0, "minor": 0, "suggestion": 0,
    }
    for item in all_items:
        if item.severity in summary:
            summary[item.severity] += 1

    # highest_verdict: P0 > P1 > P2 > APPROVE（verdict="" は無視）
    verdict_priority = {"P0": 0, "P1": 1, "P2": 2, "APPROVE": 3}
    highest = "APPROVE"
    for r in reviews:
        if verdict_priority.get(r.verdict, 99) < verdict_priority.get(highest, 99):
            highest = r.verdict

    return MergedReviewReport(
        reviews=reviews,
        all_items=all_items,
        summary=summary,
        highest_verdict=highest,
    )


# ---------------------------------------------------------------------------
# 1-H. format_merged_report
# ---------------------------------------------------------------------------

def format_merged_report(report: MergedReviewReport, rev: str) -> str:
    """MergedReviewReport を §5.6 の Markdown フォーマットに変換する。"""
    lines: list[str] = []
    lines.append(f"# Rev{rev} Review Integration Report")
    lines.append("## Summary")

    reviewer_summary = ", ".join(
        f"{r.reviewer} ({r.verdict})" for r in report.reviews
    )
    lines.append(f"- Reviewers: {reviewer_summary}")
    lines.append(
        f"- Critical: {report.summary['critical']} items, "
        f"Major: {report.summary['major']} items, "
        f"Minor: {report.summary['minor']} items, "
        f"Suggestion: {report.summary['suggestion']} items"
    )

    lines.append("## All Findings (by severity)")
    for item in report.all_items:
        sev_label = item.severity.capitalize()
        lines.append(f"### {sev_label} — {item.normalized_id}: {item.title} ({item.section})")
        lines.append(item.description)
        if item.suggestion:
            lines.append(f"**Suggestion:** {item.suggestion}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 1-I. build_review_history_entry
# ---------------------------------------------------------------------------

def build_review_history_entry(spec_config: dict, now: datetime) -> dict:
    """current_reviews から review_history エントリ（§12.2）を生成する。

    想定呼び出しコンテキスト:
        パイプライン JSON から読み込み後に呼ばれる。items は JSON 復元後の
        dict リスト（SpecReviewItem ではない）。parse直後のデータクラスでは
        呼ばない。
    """
    cr = spec_config.get("current_reviews", {})
    entries = cr.get("entries", {})

    reviews_summary: dict[str, dict] = {}
    merged_counts = {"critical": 0, "major": 0, "minor": 0, "suggestion": 0}

    for reviewer, entry in entries.items():
        if entry.get("status") != "received":
            continue
        # items は dict のリスト（JSON復元後）
        counts: dict[str, int] = {"critical": 0, "major": 0, "minor": 0, "suggestion": 0}
        for item in entry.get("items", []):
            if not isinstance(item, dict):
                continue
            sev = item.get("severity", "suggestion")
            if sev in counts:
                counts[sev] += 1
                merged_counts[sev] += 1
        reviews_summary[reviewer] = {
            "verdict": entry.get("verdict"),
            "counts": counts,
        }

    return {
        "rev": spec_config.get("current_rev", "1"),
        "rev_index": spec_config.get("rev_index", 1),
        "reviews": reviews_summary,
        "merged_counts": merged_counts,
        "commit": spec_config.get("last_commit"),
        "timestamp": now.isoformat(),
    }
