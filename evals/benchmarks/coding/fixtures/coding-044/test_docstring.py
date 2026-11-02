import ast

SRC = open("compress.py", encoding="utf-8").read()
TARGET = "zlib_size"


def _find():
    tree = ast.parse(SRC)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == TARGET:
            return node
    raise AssertionError(f"没找到函数 {TARGET}")


def test_function_exists():
    assert _find() is not None


def test_docstring_present():
    func = _find()
    doc = ast.get_docstring(func)
    assert doc, "函数缺 docstring"
    assert "Args" in doc and "Returns" in doc, f"docstring 缺 Args/Returns: {doc}"
