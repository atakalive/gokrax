"""共通プロンプトパーツ。"""


def no_interrupt_header() -> str:
    return "【指示】このタスクは中断せず最後まで一気に完了してください。途中で確認を求めないこと。"


def no_interrupt_footer(action: str = "レビュー完了・結果の提出") -> str:
    return f"【重要】{action}まで、中断せず一気に完了すること。"


def submit_instruction(cli_path: str, command: str) -> str:
    return f"python3 {cli_path} {command}"
