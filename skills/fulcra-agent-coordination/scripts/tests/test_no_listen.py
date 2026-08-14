import ast
from pathlib import Path


CLI = Path(__file__).resolve().parents[1] / "coord_engine" / "cli.py"


def test_retired_listener_has_no_handler_or_parser_registration():
    tree = ast.parse(CLI.read_text())
    functions = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }
    listen_parsers = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not isinstance(function, ast.Attribute) or function.attr != "add_parser":
            continue
        if node.args and isinstance(node.args[0], ast.Constant):
            if node.args[0].value == "listen":
                listen_parsers.append(node.lineno)

    assert "cmd_listen" not in functions
    assert listen_parsers == []
