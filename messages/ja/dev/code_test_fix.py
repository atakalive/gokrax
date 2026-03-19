"""CODE_TEST_FIX ステートのプロンプト。

Variables:
    project: str        - プロジェクト名
    test_output: str    - テスト失敗出力（切り詰め済み）
    retry_count: int    - 現在のリトライ回数（テスト失敗累積回数）
    max_retry: int      - 最大リトライ回数（MAX_TEST_RETRY）
    GOKRAX_CLI: str     - gokrax CLIパス
"""


def cc_test_fix(
    project: str,
    test_output: str,
    retry_count: int,
    max_retry: int,
    **_kw: object,
) -> str:
    """CC に送信するテスト修正プロンプト。"""
    return (
        f"テストが失敗しました（{retry_count}/{max_retry}回目）。\n"
        f"以下のテスト出力を読み、コードを修正してテストを通してください。\n\n"
        f"```\n{test_output}\n```\n\n"
        f"修正後、git commit してください。\n"
        f"テストコード自体の修正は最終手段です。まずプロダクトコードの修正を試みてください。\n"
        f"テストの skip 化や snapshot の無条件更新は禁止です。"
    )


def transition(
    project: str,
    test_output: str,
    retry_count: int,
    max_retry: int,
    GOKRAX_CLI: str,
    **_kw: object,
) -> str:
    """テスト修正フェーズの実装者向け通知メッセージ。"""
    return (
        f"テスト修正フェーズ（{retry_count}/{max_retry}回目）\n"
        f"テストが失敗しました。CCが自動修正を試みます。\n\n"
        f"```\n{test_output[-2000:]}\n```"
    )


def nudge(**_kw: object) -> str:
    """CODE_TEST_FIX 催促メッセージ。"""
    return (
        "[Remind] テスト修正作業を進め、完了してください。"
    )
