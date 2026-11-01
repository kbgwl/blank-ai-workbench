import ast

SRC = open("calculator.py", encoding="utf-8").read()
tree = ast.parse(SRC)
func = tree.body[0]

def test_docstring_present():
    assert isinstance(func.body[0], ast.Expr), "第一句必须是 docstring"
    doc = ast.get_docstring(func)
    assert doc and "Args" in doc and "Returns" in doc, f"docstring 缺 Args/Returns: {doc}"
