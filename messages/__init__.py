"""gokrax プロンプト外部化モジュール。

Usage:
    from messages import render
    msg = render("spec.review", "initial", project="Foo", spec_path="docs/spec.md", ...)
"""

import importlib
import importlib.util

try:
    from config import PROMPT_LANG
except ImportError:
    PROMPT_LANG = "en"  # デフォルトは英語


def render(template: str, macro: str, lang: str | None = None, **kwargs) -> str:
    """テンプレートのmacro関数を呼び出してプロンプト文字列を返す。

    Args:
        template: ステート名（例: "spec_review", "design_revise"）
        macro: 関数名（例: "initial", "nudge", "notify_start"）
        lang: 言語コード（デフォルト: config.PROMPT_LANG or "en"）
        **kwargs: テンプレート関数に渡す引数
    """
    lang = lang or PROMPT_LANG
    # カスタムテンプレートを優先（find_spec で存在確認し、内部エラーは浮上させる）
    custom_name = f"messages_custom.{lang}.{template}"
    try:
        spec = importlib.util.find_spec(custom_name)
    except (ModuleNotFoundError, ValueError):
        spec = None
    if spec is not None:
        custom_mod = importlib.import_module(custom_name)
        fn = getattr(custom_mod, macro, None)
        if fn is not None:
            return fn(**kwargs)
    # デフォルトにフォールバック
    mod = importlib.import_module(f"messages.{lang}.{template}")
    return getattr(mod, macro)(**kwargs)
