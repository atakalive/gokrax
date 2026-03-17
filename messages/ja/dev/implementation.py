"""IMPLEMENTATION ステートのプロンプト（CC plan/impl/resume）。

CC実行用のプロンプト文字列を生成する。
テストベースラインの埋め込みロジック自体は watchdog.py に残す（subprocess依存のため）。

Variables:
    issues_block: str          - Issue本文テキスト（### #{N}: title\\n{body} の連結）
    closes: str                - "Closes #1 Closes #2 ..." 文字列
    comment_line: str          - オーナーコメント行（空文字 or "{OWNER_NAME}からの要望: ...\\n\\n"）
    test_baseline_section: str - テストベースラインセクション（空文字 or "\\n\\n## テストベースライン..."）
"""


def cc_plan(
    issues_block: str,
    closes: str,
    comment_line: str,
    **_kw,
) -> str:
    """CC Plan フェーズ用プロンプト。"""
    return (
        f"以下のIssueを実装する計画を立ててください。\n"
        f"{comment_line}"
        f"\n{issues_block}\n\n"
        f"コミットメッセージに {closes} を必ず含めること。\n\n"
        f"計画を立てた後、最後に以下のフォーマットで実装申し送りを出力せよ:\n\n"
        f"## 実装申し送り\n"
        f"### 変更対象\n"
        f"- ファイルパスと変更内容（箇条書き）\n\n"
        f"### 触るな\n"
        f"- 既存コードで変更してはいけない箇所・理由\n\n"
        f"### 罠・エッジケース\n"
        f"- 実装時に注意すべき点（見つけたもの全て）\n\n"
        f"### テスト観点\n"
        f"- テストすべきケース（正常系・異常系・境界値）"
    )


def cc_impl_resume(
    closes: str,
    scope_warning: str,
    test_baseline_section: str,
    **_kw,
) -> str:
    """CC Impl フェーズ用プロンプト（Plan OK 後の実装指示 = resume session）。"""
    return (
        f"計画OK。実装して commit して。コミットメッセージに {closes} を必ず含めること。"
        f"{scope_warning}"
        f"{test_baseline_section}"
    )


def cc_impl_skip_plan(
    issues_block: str,
    closes: str,
    comment_line: str,
    scope_warning: str,
    test_baseline_section: str,
    **_kw,
) -> str:
    """CC Impl フェーズ用プロンプト（Plan スキップ = 直接実装）。"""
    return (
        f"以下のIssueを実装してください。\n"
        f"{comment_line}"
        f"\n{issues_block}\n\n"
        f"コミットメッセージに {closes} を必ず含めること。"
        f"{scope_warning}"
        f"{test_baseline_section}"
    )


def scope_warning_normal(**_kw) -> str:
    """通常モードのスコープ警告（Plan → Impl）。"""
    return (
        "\n\n⚠️ スコープ厳守: Issue本文に記載された変更のみを実装せよ。"
        "Issue本文に記載のない改善・リファクタ・バグ修正は一切行うな。"
    )


def scope_warning_skip_plan(**_kw) -> str:
    """skip_plan モードのスコープ警告（直接 Impl）。"""
    return (
        "\n\n⚠️ スコープ厳守: Issue本文に記載された変更対象ファイル・変更内容のみを実装せよ。"
        "「変更しないファイル」に記載されたファイルは絶対に変更するな。"
        "Issue本文に記載のない改善・リファクタ・バグ修正は一切行うな。"
    )


def test_baseline_pass(bl_output: str, **_kw) -> str:
    """テストベースライン（全パス時）。"""
    return (
        "\n\n## テストベースライン（impl 開始前の状態）\n"
        f"exit_code: 0 (全パス)\n\n{bl_output}\n\n"
        "あなたの変更でテストを壊さないこと。"
    )


def test_baseline_fail(bl_exit: int, bl_output: str, **_kw) -> str:
    """テストベースライン（一部失敗時）。"""
    return (
        "\n\n## テストベースライン（impl 開始前の状態）\n"
        f"exit_code: {bl_exit} (一部失敗)\n\n{bl_output}\n\n"
        "⚠️ 上記の失敗は impl 開始前から存在するもの。\n"
        "あなたの変更で新たに壊してはいけない。"
    )


def cc_commit_retry(closes: str, **_kw) -> str:
    """CC コミット未検出時のリトライ指示（run_cc スクリプト内）。"""
    return (
        "実装は完了しているが git commit されていない。以下のコマンドを実行せよ:\n\n"
        f'  git add -A\n'
        f'  git commit -m "feat({closes}): <変更内容の要約>"\n\n'
        f"コミットメッセージには {closes} を必ず含めること。\n"
        "変更すべきファイルがワーキングツリーにない場合は、Issue本文の変更対象を読み直して実装してからコミットせよ。"
    )


def nudge(**_kw) -> str:
    """IMPLEMENTATION 催促メッセージ。"""
    return (
        "[Remind] 実装を進め、完了してください。\n"
        "devbar commit --pj <project> --issue <N> --hash <commit> でコミットを報告してください。"
    )


# ---------------------------------------------------------------------------
# Discord通知（CC進捗、bashスクリプト内の _notify 呼び出し用）
# ---------------------------------------------------------------------------

def notify_cc_plan_start(project: str, plan_model: str, q_tag: str = "", **_kw) -> str:
    """CC Plan 開始通知。"""
    return f"{q_tag}[{project}] 📋 CC Plan 開始 (model: {plan_model})"


def notify_cc_plan_done(project: str, q_tag: str = "", **_kw) -> str:
    """CC Plan 完了通知。"""
    return f"{q_tag}[{project}] ✅ CC Plan 完了"


def notify_cc_impl_start(project: str, impl_model: str, q_tag: str = "", **_kw) -> str:
    """CC Impl 開始通知。"""
    return f"{q_tag}[{project}] 🔨 CC Impl 開始 (model: {impl_model})"


def notify_cc_impl_start_skip_plan(project: str, impl_model: str, q_tag: str = "", **_kw) -> str:
    """CC Impl 開始通知（plan skip）。"""
    return f"{q_tag}[{project}] 🔨 CC Impl 開始 (plan skip, model: {impl_model})"


def notify_cc_impl_done(project: str, q_tag: str = "", **_kw) -> str:
    """CC Impl 完了通知。"""
    return f"{q_tag}[{project}] ✅ CC Impl 完了"


def notify_cc_no_commit_retry(project: str, retry: str, q_tag: str = "", **_kw) -> str:
    """CC コミット未検出リトライ通知。"""
    return f"{q_tag}[{project}] ⚠️ コミット未検出 — CC にリトライ指示 ({retry})"


def notify_cc_no_commit_blocked(project: str, q_tag: str = "", **_kw) -> str:
    """CC コミット作成失敗 → BLOCKED 通知。"""
    return f"{q_tag}[{project}] ❌ CC がコミットを作成しなかった（2回リトライ後）→ BLOCKED"


def notify_cc_start_failed(project: str, error: str, **_kw) -> str:
    """CC 起動失敗通知。"""
    return f"[{project}] ⚠️ CC起動失敗: {error}"
