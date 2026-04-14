from config import LOG_FILE
from notify import mask_agent_name
from pipeline_io import now_iso


# Verdict severity for dispute resolution (Issue #86)
VERDICT_SEVERITY = {"REJECT": 3, "P0": 3, "P1": 2, "P2": 1, "APPROVE": 0}

# Risk display labels for assessment title tags
RISK_DISPLAY = {"n/a": "", "none": "No Risk", "low": "Low Risk", "high": "High Risk"}


def parse_issue_args(raw: list[str]) -> list[int]:
    """Flatten comma-separated and/or space-separated issue args into int list.

    Examples:
        parse_issue_args(["33,34"]) -> [33, 34]
        parse_issue_args(["33", "34"]) -> [33, 34]
        parse_issue_args(["33,34", "35"]) -> [33, 34, 35]
        parse_issue_args(["33"]) -> [33]

    Raises:
        SystemExit: on non-numeric element
    """
    result: list[int] = []
    for item in raw:
        for part in str(item).split(","):
            part = part.strip()
            if part:
                try:
                    result.append(int(part))
                except ValueError:
                    raise SystemExit(f"Invalid issue number: {part!r}")
    if not result:
        raise SystemExit("No issue numbers provided")
    return result


def _log(msg: str) -> None:
    """LOG_FILE にメッセージを追記。失敗は無視。"""
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"{now_iso()} {msg}\n")
    except Exception:
        pass


def _masked_reviewer(
    reviewer: str,
    reviewer_number_map: dict[str, int] | None,
) -> str:
    """print 出力用のマスク済みレビュアー名を返す。"""
    return mask_agent_name(reviewer, reviewer_number_map=reviewer_number_map)


def _reset_to_idle(data: dict) -> None:
    """data を IDLE 状態にリセットする（batch クリア + フラグ除去 + リソース解放）。

    state と history の更新は呼び出し側で行う。
    spec_mode のクリーンアップは行わない（それは cmd_spec_stop の責務）。
    クリーンアップの実体は _cleanup_batch_state() に委譲。
    """
    from engine.cleanup import _cleanup_batch_state
    pj = data.get("project", "")
    _cleanup_batch_state(data, pj)
