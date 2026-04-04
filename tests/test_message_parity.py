"""test_message_parity.py — メッセージ外部化 Step2: 差し替え前後の出力一致テスト

差し替え前（旧関数が存在する状態）でも差し替え後（render()に置き換え済み）でも
両方のテストが通ることを確認する。

watchdog.py の削除対象関数については、比較用のコピーをこのファイル内に保持する。
"""

import pytest
from pathlib import Path
from config import GOKRAX_CLI, PIPELINES_DIR
from messages import render
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
from spec_revise import build_revise_prompt, build_self_review_prompt, make_rev_path


# ---------------------------------------------------------------------------
# 旧 watchdog.py 関数のコピー（削除前の参照実装）
# ---------------------------------------------------------------------------

def _old_build_spec_review_prompt_initial(
    project: str,
    spec_path: str,
    current_rev: str,
    spec_config: dict,
) -> str:
    """旧 watchdog._build_spec_review_prompt_initial のコピー。"""
    pipelines_dir = spec_config.get("pipelines_dir") or str(PIPELINES_DIR)
    spec_name = Path(spec_path).stem
    return f"""【指示】このタスクは中断せず最後まで一気に完了してください。途中で確認を求めないこと。

以下の仕様書をレビューしてください。**やりすぎレビュー**を依頼します。

プロジェクト: {project}
仕様書: {spec_path} (rev{current_rev})

## レビュー指示
- 重篤度を必ず付与: 🔴 Critical (P0) / 🟠 Major (P1) / 🟡 Minor / 💡 Suggestion
- セクション番号を明記（例: §6.2）
- 擬似コード間の整合性に特に注意
- 既存gokraxコードベースとの整合性も確認
- ステートマシン遷移の抜け穴・デッドロックを探せ
- YAMLブロックは応答内で**1つだけ**
- verdict の選び方: critical → P0, major → P1, minor/suggestion → P2。指摘ゼロの場合のみ APPROVE

## 出力フォーマット
```yaml
verdict: APPROVE | P0 | P1 | P2
items:
  - id: C-1
    severity: critical | major | minor | suggestion
    section: "§6.2"
    title: "タイトル"
    description: "説明"
    suggestion: "修正案"
```

## レビュー結果の投入手順
1. ワークスペース内にYAMLファイルを保存（パスは任意）
2. 以下のコマンドで投入:
```bash
{GOKRAX_CLI} spec review-submit --pj {project} --reviewer <YOUR_NAME> --file <保存したファイルのパス>
```

ファイルは素のYAMLでも、上記「出力フォーマット」の ```yaml ... ``` ブロックを含むMarkdownでも可。

【重要】レビュー完了・結果の提出まで、中断せず一気に完了すること。"""


def _old_build_spec_review_prompt_revision(
    project: str,
    spec_path: str,
    current_rev: str,
    spec_config: dict,
    data: dict,
) -> str:
    """旧 watchdog._build_spec_review_prompt_revision のコピー。"""
    pipelines_dir = spec_config.get("pipelines_dir") or str(PIPELINES_DIR)
    spec_name = Path(spec_path).stem
    last_commit = spec_config.get("last_commit") or "unknown"
    last_changes = spec_config.get("last_changes") or {}
    added = last_changes.get("added_lines", "?")
    removed = last_changes.get("removed_lines", "?")
    changelog = last_changes.get("changelog_summary", "変更履歴なし")
    return f"""【指示】このタスクは中断せず最後まで一気に完了してください。途中で確認を求めないこと。

以下の仕様書の改訂版をレビューしてください。

プロジェクト: {project}
仕様書: {spec_path} (rev{current_rev})
前回からの変更: +{added}行, -{removed}行
前回commit: {last_commit}

## 前回レビューからの変更点
{changelog}

## レビュー指示
- 前回の指摘が適切に反映されているか確認
- 新たに追加された部分に問題がないか確認
- 重篤度・セクション番号・YAMLフォーマットは前回と同様
- YAMLブロックは応答内で**1つだけ**
- verdict の選び方: critical → P0, major → P1, minor/suggestion → P2。指摘ゼロの場合のみ APPROVE

## レビュー結果の投入手順
1. ワークスペース内にYAMLファイルを保存（パスは任意）
2. 以下のコマンドで投入:
```bash
{GOKRAX_CLI} spec review-submit --pj {project} --reviewer <YOUR_NAME> --file <保存したファイルのパス>
```

ファイルは素のYAMLでも、上記「出力フォーマット」の ```yaml ... ``` ブロックを含むMarkdownでも可。

【重要】レビュー完了・結果の提出まで、中断せず一気に完了すること。"""


def _old_build_spec_review_nudge_msg(
    project: str, current_rev: str, spec_path: str, reviewer: str
) -> str:
    """旧 watchdog._build_spec_review_nudge_msg のコピー。"""
    return (
        f"[Remind] {project} spec rev{current_rev} のレビューが未完了です。\n"
        f"仕様書: {spec_path}\n"
        f"以下のコマンドでレビュー結果を提出してください:\n"
        f"{GOKRAX_CLI} spec review-submit --pj {project} --reviewer {reviewer} --file <YAMLファイルパス>"
    )


def _old_build_spec_revise_nudge_msg(project: str, current_rev: str) -> str:
    """旧 watchdog._build_spec_revise_nudge_msg のコピー。"""
    return (
        f"[Remind] {project} spec rev{current_rev} のリバイス作業が未完了です。\n"
        f"レビュー指摘を反映し、以下のコマンドで完了報告してください:\n"
        f"{GOKRAX_CLI} spec revise-submit --pj {project} --file <完了報告YAMLファイルパス>"
    )


# ---------------------------------------------------------------------------
# § 1. watchdog.py プロンプト生成関数 vs render()
# ---------------------------------------------------------------------------

class TestBuildSpecReviewPromptInitialParity:
    """render("spec.review", "initial", ...) の挙動テスト。

    #283 で reviewer 引数追加・ファイルパス指定に変更したため、
    旧実装との比較は廃止し、新挙動のテストに置き換え。
    """

    def test_with_reviewer(self):
        """reviewer 指定時、具体的なファイルパスがプロンプトに含まれる。"""
        result = render("spec.review", "initial",
            project="MyProject", spec_path="/docs/spec.md",
            current_rev="1", GOKRAX_CLI=GOKRAX_CLI,
            reviewer="basho",
        )
        assert "/tmp/gokrax-review/MyProject--spec-basho-rev1.yaml" in result
        assert "<YOUR_NAME>" not in result

    def test_without_reviewer(self):
        """reviewer 未指定時、<YOUR_NAME> プレースホルダにフォールバック。"""
        result = render("spec.review", "initial",
            project="MyProject", spec_path="/docs/spec.md",
            current_rev="1", GOKRAX_CLI=GOKRAX_CLI,
        )
        assert "/tmp/gokrax-review/MyProject--spec-<YOUR_NAME>-rev1.yaml" in result

    def test_sanitizes_project_name(self):
        """プロジェクト名のスラッシュ・空白がハイフンに置換される。"""
        result = render("spec.review", "initial",
            project="foo/bar baz", spec_path="/docs/spec.md",
            current_rev="1", GOKRAX_CLI=GOKRAX_CLI,
            reviewer="pascal",
        )
        assert "/tmp/gokrax-review/foo-bar-baz--spec-pascal-rev1.yaml" in result

    def test_project_embedded(self):
        result = render("spec.review", "initial",
            project="TestPJ", spec_path="/path/to/spec.md",
            current_rev="3", GOKRAX_CLI=GOKRAX_CLI,
        )
        assert "TestPJ" in result
        assert "/path/to/spec.md" in result
        assert "rev3" in result
        assert str(GOKRAX_CLI) in result


class TestBuildSpecReviewPromptRevisionParity:
    """render("spec.review", "revision", ...) の挙動テスト。

    #283 で reviewer 引数追加・ファイルパス指定に変更したため、
    旧実装との比較は廃止し、新挙動のテストに置き換え。
    """

    def test_with_reviewer(self):
        """reviewer 指定時、具体的なファイルパスがプロンプトに含まれる。"""
        result = render("spec.review", "revision",
            project="MyProject", spec_path="/docs/spec-rev2.md",
            current_rev="2", GOKRAX_CLI=GOKRAX_CLI,
            reviewer="euler",
            changelog="- fix A", added="10", removed="5",
            last_commit="abc1234",
        )
        assert "/tmp/gokrax-review/MyProject--spec-euler-rev2.yaml" in result
        assert "<YOUR_NAME>" not in result

    def test_without_reviewer(self):
        """reviewer 未指定時、<YOUR_NAME> プレースホルダにフォールバック。"""
        result = render("spec.review", "revision",
            project="MyProject", spec_path="/docs/spec-rev2.md",
            current_rev="2", GOKRAX_CLI=GOKRAX_CLI,
            changelog="- fix A", added="10", removed="5",
            last_commit="abc1234",
        )
        assert "/tmp/gokrax-review/MyProject--spec-<YOUR_NAME>-rev2.yaml" in result

    def test_sanitizes_project_name(self):
        """プロジェクト名のスラッシュ・空白がハイフンに置換される。"""
        result = render("spec.review", "revision",
            project="foo/bar baz", spec_path="/docs/spec.md",
            current_rev="1", GOKRAX_CLI=GOKRAX_CLI,
            reviewer="dijkstra",
            changelog="changes", added="1", removed="0",
            last_commit="def5678",
        )
        assert "/tmp/gokrax-review/foo-bar-baz--spec-dijkstra-rev1.yaml" in result

    def test_defaults_applied(self):
        """last_changes が空の場合、デフォルト値が埋め込まれること。"""
        result = render("spec.review", "revision",
            project="P", spec_path="/s.md",
            current_rev="3", GOKRAX_CLI=GOKRAX_CLI,
            changelog="変更履歴なし",
            added="?", removed="?",
            last_commit="unknown",
        )
        assert "変更履歴なし" in result
        assert "+?行" in result
        assert "-?行" in result
        assert "unknown" in result


# ---------------------------------------------------------------------------
# § 2. 催促関数 vs render()
# ---------------------------------------------------------------------------

class TestSpecReviewNudgeParity:
    """render("spec.review", "nudge", ...) の挙動テスト。

    #283 で nudge にもファイルパス指定を追加したため、
    旧実装との比較は廃止し、新挙動のテストに置き換え。
    """

    def test_includes_save_path(self):
        """nudge に具体的なファイルパスが含まれる。"""
        result = render("spec.review", "nudge",
            project="MyProject", current_rev="2",
            spec_path="/docs/spec.md", reviewer="reviewer1",
            GOKRAX_CLI=GOKRAX_CLI,
        )
        assert "/tmp/gokrax-review/MyProject--spec-reviewer1-rev2.yaml" in result

    def test_sanitizes_project_name(self):
        """nudge でもプロジェクト名がサニタイズされる。"""
        result = render("spec.review", "nudge",
            project="foo/bar", current_rev="1",
            spec_path="/path.md", reviewer="reviewer3",
            GOKRAX_CLI=GOKRAX_CLI,
        )
        assert "/tmp/gokrax-review/foo-bar--spec-reviewer3-rev1.yaml" in result

    def test_content(self):
        result = render("spec.review", "nudge",
            project="Proj", current_rev="1",
            spec_path="/path.md", reviewer="reviewer3",
            GOKRAX_CLI=GOKRAX_CLI,
        )
        assert "Proj" in result
        assert "rev1" in result
        assert "/path.md" in result
        assert "reviewer3" in result
        assert str(GOKRAX_CLI) in result


class TestSpecReviseNudgeParity:
    """_build_spec_revise_nudge_msg vs render("spec.revise", "nudge", ...)"""

    def test_parity_basic(self):
        project = "MyProject"
        current_rev = "3"
        old = _old_build_spec_revise_nudge_msg(project, current_rev)
        new = render("spec.revise", "nudge",
            project=project, current_rev=current_rev,
            GOKRAX_CLI=GOKRAX_CLI,
        )
        assert old == new

    def test_parity_content(self):
        new = render("spec.revise", "nudge",
            project="TestProj", current_rev="2",
            GOKRAX_CLI=GOKRAX_CLI,
        )
        assert "TestProj" in new
        assert "rev2" in new
        assert str(GOKRAX_CLI) in new


# ---------------------------------------------------------------------------
# § 3. spec_revise.py 関数 vs render()
# ---------------------------------------------------------------------------

class TestBuildRevisePromptParity:
    """build_revise_prompt vs render("spec.revise", "revise", ...)"""

    def _make_spec_config(self, spec_path: str, current_rev: str = "1", rev_index: int = 1) -> dict:
        return {
            "spec_path": spec_path,
            "current_rev": current_rev,
            "rev_index": rev_index,
        }

    def test_parity_basic(self):
        spec_path = "/docs/spec.md"
        spec_config = self._make_spec_config(spec_path, "1", 1)
        data = {"project": "MyProject"}
        merged_report_md = "## merged report\n- C-1: some issue"
        old = build_revise_prompt(spec_config, merged_report_md, data)

        project = data.get("project", "")
        current_rev = spec_config.get("current_rev", "1")
        rev_index = spec_config.get("rev_index", 1)
        next_rev = rev_index + 1
        new_spec_path = make_rev_path(spec_path, next_rev)
        new = render("spec.revise", "revise",
            project=project, spec_path=spec_path, current_rev=current_rev,
            GOKRAX_CLI=GOKRAX_CLI, next_rev=next_rev, new_spec_path=new_spec_path,
            merged_report_md=merged_report_md,
        )
        assert old == new

    def test_parity_rev2(self):
        spec_path = "/docs/spec-rev2.md"
        spec_config = self._make_spec_config(spec_path, "2", 2)
        data = {"project": "AnotherProj"}
        merged_report_md = "## rev2 report\n- M-1: big issue"
        old = build_revise_prompt(spec_config, merged_report_md, data)

        project = data.get("project", "")
        current_rev = spec_config.get("current_rev", "1")
        rev_index = spec_config.get("rev_index", 1)
        next_rev = rev_index + 1
        new_spec_path = make_rev_path(spec_path, next_rev)
        new = render("spec.revise", "revise",
            project=project, spec_path=spec_path, current_rev=current_rev,
            GOKRAX_CLI=GOKRAX_CLI, next_rev=next_rev, new_spec_path=new_spec_path,
            merged_report_md=merged_report_md,
        )
        assert old == new

    def test_raises_on_empty_spec_path(self):
        spec_config = {"spec_path": "", "current_rev": "1", "rev_index": 1}
        with pytest.raises(ValueError, match="spec_path"):
            build_revise_prompt(spec_config, "report", {"project": "P"})


class TestBuildSelfReviewPromptParity:
    """build_self_review_prompt vs render("spec.revise", "self_review", ...)"""

    def _make_spec_config(self, spec_path: str, current_rev: str = "1", last_commit: str = "abc1234") -> dict:
        return {
            "spec_path": spec_path,
            "current_rev": current_rev,
            "last_commit": last_commit,
        }

    def _build_checklist_texts(self, checklist: list[dict]) -> tuple[str, str]:
        """旧実装と同じチェックリストテキスト・YAML例文を生成。"""
        checklist_lines = []
        for item in checklist:
            checklist_lines.append(f'- **{item["id"]}**: {item["question"]}')
        checklist_text = "\n".join(checklist_lines)

        example_items = []
        for item in checklist:
            example_items.append(
                f'  - id: "{item["id"]}"\n'
                f'    result: "Yes"\n'
                f'    evidence: "（確認内容を記述）"'
            )
        example_yaml = "checklist:\n" + "\n".join(example_items)
        return checklist_text, example_yaml

    def test_parity_default_checklist(self):
        from spec_revise import DEFAULT_SELF_REVIEW_CHECKLIST
        spec_path = "/docs/spec-rev2.md"
        spec_config = self._make_spec_config(spec_path, "2", "def4567")
        data = {"project": "MyProject"}
        old = build_self_review_prompt(spec_config, data)

        project = data.get("project", "")
        new_rev = spec_config.get("current_rev", "1")
        last_commit = spec_config.get("last_commit", "unknown")
        checklist = spec_config.get("self_review_checklist", DEFAULT_SELF_REVIEW_CHECKLIST)
        checklist_text, example_yaml = self._build_checklist_texts(checklist)
        new = render("spec.revise", "self_review",
            project=project, spec_path=spec_path, current_rev=new_rev,
            GOKRAX_CLI=GOKRAX_CLI, last_commit=last_commit,
            checklist_text=checklist_text, example_yaml=example_yaml,
        )
        assert old == new

    def test_parity_custom_checklist(self):
        spec_path = "/docs/spec.md"
        custom_checklist = [
            {"id": "check_a", "question": "Question A?"},
            {"id": "check_b", "question": "Question B?"},
        ]
        spec_config = {
            "spec_path": spec_path,
            "current_rev": "1",
            "last_commit": "aabbcc1",
            "self_review_checklist": custom_checklist,
        }
        data = {"project": "CustomProj"}
        old = build_self_review_prompt(spec_config, data)

        project = data.get("project", "")
        new_rev = spec_config.get("current_rev", "1")
        last_commit = spec_config.get("last_commit", "unknown")
        checklist = spec_config.get("self_review_checklist", [])
        checklist_text, example_yaml = self._build_checklist_texts(checklist)
        new = render("spec.revise", "self_review",
            project=project, spec_path=spec_path, current_rev=new_rev,
            GOKRAX_CLI=GOKRAX_CLI, last_commit=last_commit,
            checklist_text=checklist_text, example_yaml=example_yaml,
        )
        assert old == new


# ---------------------------------------------------------------------------
# § 4. notify.py spec_notify_* 関数 vs render()
# ---------------------------------------------------------------------------

class TestSpecNotifyFunctionsParity:
    """全16個の spec_notify_* 関数 vs 対応する render() 呼び出し。"""

    def test_review_start(self):
        old = spec_notify_review_start("MyProj", "2", 3)
        new = render("spec.review", "notify_start", project="MyProj", rev="2", reviewer_count=3)
        assert old == new

    def test_review_start_int_rev(self):
        old = spec_notify_review_start("P", 5, 1)
        new = render("spec.review", "notify_start", project="P", rev=5, reviewer_count=1)
        assert old == new

    def test_review_complete(self):
        old = spec_notify_review_complete("P", "1", 2, 3, 5, 8)
        new = render("spec.review", "notify_complete",
            project="P", rev="1", critical=2, major=3, minor=5, suggestion=8)
        assert old == new

    def test_review_complete_zeros(self):
        old = spec_notify_review_complete("P", "2", 0, 0, 0, 0)
        new = render("spec.review", "notify_complete",
            project="P", rev="2", critical=0, major=0, minor=0, suggestion=0)
        assert old == new

    def test_approved(self):
        old = spec_notify_approved("MyProj", "1")
        new = render("spec.approved", "notify_approved", project="MyProj", rev="1")
        assert old == new

    def test_approved_auto(self):
        old = spec_notify_approved_auto("MyProj", "3")
        new = render("spec.approved", "notify_approved_auto", project="MyProj", rev="3")
        assert old == new

    def test_approved_forced(self):
        old = spec_notify_approved_forced("MyProj", "2", 5)
        new = render("spec.approved", "notify_approved_forced",
            project="MyProj", rev="2", remaining_p1_plus=5)
        assert old == new

    def test_stalled(self):
        old = spec_notify_stalled("MyProj", "4", 7)
        new = render("spec.stalled", "notify_stalled",
            project="MyProj", rev="4", remaining_p1_plus=7)
        assert old == new

    def test_review_failed(self):
        old = spec_notify_review_failed("MyProj", "1")
        new = render("spec.review", "notify_failed", project="MyProj", rev="1")
        assert old == new

    def test_paused(self):
        old = spec_notify_paused("MyProj", "パース失敗")
        new = render("spec.paused", "notify_paused", project="MyProj", reason="パース失敗")
        assert old == new

    def test_paused_empty_reason(self):
        old = spec_notify_paused("MyProj", "")
        new = render("spec.paused", "notify_paused", project="MyProj", reason="")
        assert old == new

    def test_revise_done(self):
        commit = "abcdef1234567890"
        old = spec_notify_revise_done("MyProj", "2", commit)
        new = render("spec.revise", "notify_done", project="MyProj", rev="2", commit=commit)
        assert old == new

    def test_revise_commit_failed(self):
        old = spec_notify_revise_commit_failed("MyProj", "1")
        new = render("spec.revise", "notify_commit_failed", project="MyProj", rev="1")
        assert old == new

    def test_revise_no_changes(self):
        old = spec_notify_revise_no_changes("MyProj", "3")
        new = render("spec.revise", "notify_no_changes", project="MyProj", rev="3")
        assert old == new

    def test_issue_plan_done(self):
        old = spec_notify_issue_plan_done("MyProj", 10)
        new = render("spec.issue_plan", "notify_done", project="MyProj", issue_count=10)
        assert old == new

    def test_queue_plan_done(self):
        old = spec_notify_queue_plan_done("MyProj", 4)
        new = render("spec.queue_plan", "notify_done", project="MyProj", batch_count=4)
        assert old == new

    def test_done(self):
        old = spec_notify_done("MyProj")
        new = render("spec.done", "notify_done", project="MyProj")
        assert old == new

    def test_failure_with_detail(self):
        old = spec_notify_failure("MyProj", "送信失敗", "agent=foo")
        new = render("spec.paused", "notify_failure",
            project="MyProj", kind="送信失敗", detail="agent=foo")
        assert old == new

    def test_failure_without_detail(self):
        old = spec_notify_failure("MyProj", "送信失敗")
        new = render("spec.paused", "notify_failure",
            project="MyProj", kind="送信失敗", detail="")
        assert old == new

    def test_self_review_failed(self):
        old = spec_notify_self_review_failed("MyProj", 3)
        new = render("spec.revise", "notify_self_review_failed",
            project="MyProj", failed_count=3)
        assert old == new


# ---------------------------------------------------------------------------
# dev mode: merge_summary_sent — language-independent tests
# ---------------------------------------------------------------------------

class TestDevMergeSummaryMessages:
    """format_merge_summary の分岐テスト（言語非依存）。"""

    @staticmethod
    def _make_batch():
        return [{"issue": 1, "title": "Test", "commit": "abc123",
                 "code_reviews": {"reviewer1": {"verdict": "APPROVE", "summary": "ok"}}}]

    def test_automerge_excludes_footer(self):
        """automerge=True → MERGE_SUMMARY_FOOTER が含まれない"""
        from config import MERGE_SUMMARY_FOOTER
        content = render("dev.merge_summary_sent", "format_merge_summary",
            project="P", batch=self._make_batch(), automerge=True,
            MERGE_SUMMARY_FOOTER=MERGE_SUMMARY_FOOTER,
        )
        assert MERGE_SUMMARY_FOOTER not in content

    def test_no_automerge_includes_footer(self):
        """automerge=False → MERGE_SUMMARY_FOOTER がそのまま含まれる"""
        from config import MERGE_SUMMARY_FOOTER
        content = render("dev.merge_summary_sent", "format_merge_summary",
            project="P", batch=self._make_batch(), automerge=False,
            MERGE_SUMMARY_FOOTER=MERGE_SUMMARY_FOOTER,
        )
        assert MERGE_SUMMARY_FOOTER in content

    def test_automerge_and_no_automerge_differ(self):
        """automerge フラグで出力が分岐する"""
        from config import MERGE_SUMMARY_FOOTER
        batch = self._make_batch()
        on = render("dev.merge_summary_sent", "format_merge_summary",
            project="P", batch=batch, automerge=True,
            MERGE_SUMMARY_FOOTER=MERGE_SUMMARY_FOOTER,
        )
        off = render("dev.merge_summary_sent", "format_merge_summary",
            project="P", batch=batch, automerge=False,
            MERGE_SUMMARY_FOOTER=MERGE_SUMMARY_FOOTER,
        )
        assert on != off

    def test_queue_mode_prefix(self):
        """queue_mode=True → プロジェクト名の前に [Queue] が付く"""
        from config import MERGE_SUMMARY_FOOTER
        content = render("dev.merge_summary_sent", "format_merge_summary",
            project="P", batch=self._make_batch(), queue_mode=True,
            MERGE_SUMMARY_FOOTER=MERGE_SUMMARY_FOOTER,
        )
        assert "[Queue]" in content

    def test_no_queue_mode_no_prefix(self):
        """queue_mode=False → [Queue] が含まれない"""
        from config import MERGE_SUMMARY_FOOTER
        content = render("dev.merge_summary_sent", "format_merge_summary",
            project="P", batch=self._make_batch(), queue_mode=False,
            MERGE_SUMMARY_FOOTER=MERGE_SUMMARY_FOOTER,
        )
        assert "[Queue]" not in content

    def test_batch_items_in_output(self):
        """バッチの issue番号・commit が出力に含まれる"""
        from config import MERGE_SUMMARY_FOOTER
        batch = [{"issue": 42, "title": "Fix bug", "commit": "def456"}]
        content = render("dev.merge_summary_sent", "format_merge_summary",
            project="P", batch=batch, MERGE_SUMMARY_FOOTER=MERGE_SUMMARY_FOOTER,
        )
        assert "#42" in content
        assert "def456" in content

    def test_verdict_emoji_mapping(self):
        """各verdict値に対応する絵文字が出力される"""
        from config import MERGE_SUMMARY_FOOTER
        batch = [{"issue": 1, "title": "T", "commit": "c",
                  "code_reviews": {
                      "r1": {"verdict": "APPROVE", "summary": ""},
                      "r2": {"verdict": "P0", "summary": ""},
                      "r3": {"verdict": "P1", "summary": ""},
                      "r4": {"verdict": "P2", "summary": ""},
                  }}]
        content = render("dev.merge_summary_sent", "format_merge_summary",
            project="P", batch=batch, MERGE_SUMMARY_FOOTER=MERGE_SUMMARY_FOOTER,
        )
        assert "🟢" in content  # APPROVE
        assert "🔴" in content  # P0
        assert "🟡" in content  # P1
        assert "🔵" in content  # P2
