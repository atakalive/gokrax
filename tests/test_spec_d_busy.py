"""tests/test_spec_d_busy.py — #327 verification: D branch on BUSY.

Ensures that when send returns SendResult.BUSY for the D (revise resend)
branch:
  - the retry counter is decremented (no consumption)
  - busy_since_key is initialized on first BUSY
  - pending state is rolled back so the next tick re-enters the branch
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.backend_types import SendResult  # noqa: E402
from engine.fsm_spec import (  # noqa: E402
    _check_spec_revise,
    _apply_spec_action,
)

LOCAL_TZ = timezone(timedelta(hours=9))


def _now():
    return datetime(2026, 4, 24, 12, 0, 0, tzinfo=LOCAL_TZ)


def _make_pipeline(tmp_pipelines, state, spec_config):
    pj_path = tmp_pipelines / "test-pj.json"
    pj_data = {
        "project": "test-pj",
        "state": state,
        "enabled": True,
        "batch": [],
        "spec_mode": True,
        "spec_config": spec_config,
        "history": [],
    }
    pj_path.write_text(json.dumps(pj_data))
    return pj_path


# ---------------------------------------------------------------------------
# D: revise resend, send BUSY → counter rolled back + busy_since set
# ---------------------------------------------------------------------------

class TestDBusy:
    def test_d_busy_rolls_back_retries_and_sets_busy_since(self, tmp_pipelines):
        """D branch: initial revise send with pending reviews → BUSY rolls back."""
        sc = {
            "spec_implementer": "impl1",
            "spec_path": "docs/spec.md",
            "current_rev": "2",
            "_revise_sent": None,
            "_revise_send_retries": 0,
            "review_requests": {},
            "current_reviews": {
                "entries": {
                    "r1": {
                        "verdict": "P0",
                        "items": [{"id": "i1", "severity": "critical",
                            "section": "s", "title": "t",
                            "description": "d", "suggestion": "s"}],
                        "raw_text": "", "parse_success": True, "status": "received",
                    },
                },
            },
            "retry_counts": {},
        }
        pj_path = _make_pipeline(tmp_pipelines, "SPEC_REVISE", sc)
        pj_data = json.loads(pj_path.read_text())

        action = _check_spec_revise(sc, _now(), pj_data)
        assert action.send_to and "impl1" in action.send_to, "D branch not triggered"
        action.expected_state = "SPEC_REVISE"

        with patch("engine.fsm_spec.send_to_agent_with_status",
                   return_value=SendResult.BUSY), \
             patch("engine.fsm_spec.notify_discord"):
            _apply_spec_action(pj_path, action, _now(), pj_data)

        result = json.loads(pj_path.read_text())
        sc_out = result["spec_config"]
        # Retry count rolled back to original
        assert sc_out.get("_revise_send_retries", 0) == 0
        # busy_since_key initialized
        assert sc_out.get("_revise_busy_since") is not None
        # _revise_sent rolled back by send_failure_rollback
        assert sc_out.get("_revise_sent") is None
