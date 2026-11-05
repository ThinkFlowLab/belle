"""Only the deletion of an expired detached session's conversation and of an idle attached session calls ``Backend.forget``."""

import ast
from pathlib import Path

SOURCE = Path(__file__).parents[1] / "src" / "belle_agent"


def test_forget_has_named_callers_only() -> None:
    callers = []
    for path in sorted(SOURCE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            # Backend.forget takes the conversation, by position or by keyword. A buffer's forget() takes nothing.
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                for call in ast.walk(node):
                    if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                            and call.func.attr == "forget" and (call.args or call.keywords)):
                        callers.append(f"{path.relative_to(SOURCE)}:{node.name}")
    assert callers == ["host/expiry.py:delete_owned_conversation", "host/expiry.py:delete_attached_session"]
