#!/usr/bin/env python3
"""messages/ja/spec/ の外部化ファイルが旧関数と出力一致するか検証する。

使い方:
    cd /mnt/s/wsl/work/project/devbar
    python3 scripts/verify_spec_messages.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from messages import render

# --- notify.py の旧関数群 ---
from notify import (
    spec_notify_review_start,
    spec_notify_review_complete,
    spec_notify_approved,
    spec_notify_approved_auto,
    spec_notify_approved_forced,
    spec_notify_stalled,
    spec_notify_review_failed,
    spec_notify_paused,
    spec_notify_revise_done,
    spec_notify_revise_commit_failed,
    spec_notify_revise_no_changes,
    spec_notify_issue_plan_done,
    spec_notify_queue_plan_done,
    spec_notify_done,
    spec_notify_failure,
    spec_notify_self_review_failed,
)

# --- watchdog.py の旧関数群 ---
from watchdog import (
    _build_spec_review_prompt_initial,
    _build_spec_review_prompt_revision,
)

# --- spec_revise.py の旧関数群 ---
from spec_revise import (
    build_revise_prompt,
    build_self_review_prompt,
)

DEVBAR_CLI = "/home/ataka/.openclaw/shared/bin/devbar"

# テスト用ダミーデータ
TEST_PROJECT = "test-project"
TEST_SPEC_PATH = "docs/spec-rev3.md"
TEST_REV = "3"
TEST_SPEC_CONFIG = {
    "pipelines_dir": "/home/ataka/.openclaw/shared/pipelines",
    "last_commit": "abc1234",
    "last_changes": {
        "added_lines": 42,
        "removed_lines": 10,
        "changelog_summary": "§6.2 の擬似コードを修正",
    },
}
TEST_DATA = {}

passed = 0
failed = 0
errors = []


def check(name: str, old_val: str, new_val: str):
    global passed, failed
    if old_val == new_val:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        errors.append(name)
        print(f"  ❌ {name}")
        # 差分の最初の違いを表示
        for i, (a, b) in enumerate(zip(old_val, new_val)):
            if a != b:
                ctx = 40
                print(f"     First diff at char {i}:")
                print(f"     OLD: ...{old_val[max(0,i-ctx):i+ctx]}...")
                print(f"     NEW: ...{new_val[max(0,i-ctx):i+ctx]}...")
                break
        else:
            if len(old_val) != len(new_val):
                print(f"     Length diff: old={len(old_val)}, new={len(new_val)}")


# === notify.py 関数 ===
print("\n=== notify.py spec_notify_* ===")

check("review_start",
      spec_notify_review_start(TEST_PROJECT, TEST_REV, 3),
      render("spec.review", "notify_start", project=TEST_PROJECT, rev=TEST_REV, reviewer_count=3))

check("review_complete",
      spec_notify_review_complete(TEST_PROJECT, TEST_REV, 2, 3, 1, 5),
      render("spec.review", "notify_complete", project=TEST_PROJECT, rev=TEST_REV,
             critical=2, major=3, minor=1, suggestion=5))

check("approved",
      spec_notify_approved(TEST_PROJECT, TEST_REV),
      render("spec.approved", "notify_approved", project=TEST_PROJECT, rev=TEST_REV))

check("approved_auto",
      spec_notify_approved_auto(TEST_PROJECT, TEST_REV),
      render("spec.approved", "notify_approved_auto", project=TEST_PROJECT, rev=TEST_REV))

check("approved_forced",
      spec_notify_approved_forced(TEST_PROJECT, TEST_REV, 4),
      render("spec.approved", "notify_approved_forced", project=TEST_PROJECT, rev=TEST_REV,
             remaining_p1_plus=4))

check("stalled",
      spec_notify_stalled(TEST_PROJECT, TEST_REV, 2),
      render("spec.stalled", "notify_stalled", project=TEST_PROJECT, rev=TEST_REV,
             remaining_p1_plus=2))

check("review_failed",
      spec_notify_review_failed(TEST_PROJECT, TEST_REV),
      render("spec.review", "notify_failed", project=TEST_PROJECT, rev=TEST_REV))

check("paused",
      spec_notify_paused(TEST_PROJECT, "パース失敗"),
      render("spec.paused", "notify_paused", project=TEST_PROJECT, reason="パース失敗"))

check("revise_done",
      spec_notify_revise_done(TEST_PROJECT, TEST_REV, "abc1234def"),
      render("spec.revise", "notify_done", project=TEST_PROJECT, rev=TEST_REV, commit="abc1234def"))

check("revise_commit_failed",
      spec_notify_revise_commit_failed(TEST_PROJECT, TEST_REV),
      render("spec.revise", "notify_commit_failed", project=TEST_PROJECT, rev=TEST_REV))

check("revise_no_changes",
      spec_notify_revise_no_changes(TEST_PROJECT, TEST_REV),
      render("spec.revise", "notify_no_changes", project=TEST_PROJECT, rev=TEST_REV))

check("issue_plan_done",
      spec_notify_issue_plan_done(TEST_PROJECT, 8),
      render("spec.issue_plan", "notify_done", project=TEST_PROJECT, issue_count=8))

check("queue_plan_done",
      spec_notify_queue_plan_done(TEST_PROJECT, 3),
      render("spec.queue_plan", "notify_done", project=TEST_PROJECT, batch_count=3))

check("done",
      spec_notify_done(TEST_PROJECT),
      render("spec.done", "notify_done", project=TEST_PROJECT))

check("failure",
      spec_notify_failure(TEST_PROJECT, "YAMLパース失敗", "line 42"),
      render("spec.paused", "notify_failure", project=TEST_PROJECT, kind="YAMLパース失敗", detail="line 42"))

check("failure_no_detail",
      spec_notify_failure(TEST_PROJECT, "送信失敗"),
      render("spec.paused", "notify_failure", project=TEST_PROJECT, kind="送信失敗"))

check("self_review_failed",
      spec_notify_self_review_failed(TEST_PROJECT, 5),
      render("spec.revise", "notify_self_review_failed", project=TEST_PROJECT, failed_count=5))


# === watchdog.py レビュープロンプト ===
print("\n=== watchdog.py spec review messages ===")

check("review_initial",
      _build_spec_review_prompt_initial(TEST_PROJECT, TEST_SPEC_PATH, TEST_REV, TEST_SPEC_CONFIG),
      render("spec.review", "initial", project=TEST_PROJECT, spec_path=TEST_SPEC_PATH,
             current_rev=TEST_REV, DEVBAR_CLI=DEVBAR_CLI,
             pipelines_dir=TEST_SPEC_CONFIG.get("pipelines_dir"),
             spec_name="spec-rev3"))

check("review_revision",
      _build_spec_review_prompt_revision(TEST_PROJECT, TEST_SPEC_PATH, TEST_REV, TEST_SPEC_CONFIG, TEST_DATA),
      render("spec.review", "revision", project=TEST_PROJECT, spec_path=TEST_SPEC_PATH,
             current_rev=TEST_REV, DEVBAR_CLI=DEVBAR_CLI,
             last_commit="abc1234",
             added=42, removed=10,
             changelog="§6.2 の擬似コードを修正"))


# === spec_revise.py プロンプト ===
print("\n=== spec_revise.py messages ===")

# build_revise_prompt と build_self_review_prompt は引数が多いので、
# シグネチャを確認してからチェック
import inspect
print(f"  build_revise_prompt sig: {inspect.signature(build_revise_prompt)}")
print(f"  build_self_review_prompt sig: {inspect.signature(build_self_review_prompt)}")
print("  (手動確認が必要 — 引数が多く、ダミーデータの構築が複雑)")


# === サマリー ===
print(f"\n{'='*50}")
print(f"Results: {passed} passed, {failed} failed")
if errors:
    print(f"Failed: {', '.join(errors)}")
    sys.exit(1)
else:
    print("All checks passed! ✅")
    sys.exit(0)
