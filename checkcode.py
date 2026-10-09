"""Catch names used but never defined - the kind of break a refactor leaves behind.

Run: .venv\\Scripts\\python.exe checkcode.py
Also runs as part of setup.bat, so a broken edit is caught before you rely on it.
"""
import ast
import builtins
import sys
from pathlib import Path

BASE = Path(__file__).parent
TARGETS = sorted(BASE.glob("nncr/*.py")) + [
    BASE / n for n in ("app.py", "selfcheck.py", "checkkeys.py", "movekeys.py")]


MODULE_GLOBALS = {"__file__", "__name__", "__doc__", "__package__", "__spec__",
                  "__loader__", "__builtins__"}


def defined_names(tree: ast.AST) -> set:
    names = set(dir(builtins)) | MODULE_GLOBALS
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
            a = node.args
            for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs]:
                names.add(arg.arg)
            if a.vararg:
                names.add(a.vararg.arg)
            if a.kwarg:
                names.add(a.kwarg.arg)
        elif isinstance(node, ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.comprehension):
            for n in ast.walk(node.target):
                if isinstance(n, ast.Name):
                    names.add(n.id)
        elif isinstance(node, ast.Global):
            names.update(node.names)
        elif isinstance(node, ast.Lambda):
            a = node.args
            for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs]:
                names.add(arg.arg)
    return names


def main() -> int:
    problems = []
    for path in TARGETS:
        if not path.exists():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        except SyntaxError as e:
            print(f"  SYNTAX ERROR  {path.name}:{e.lineno}  {e.msg}")
            problems.append(path.name)
            continue
        known = defined_names(tree)
        used = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        missing = sorted(used - known)
        if missing:
            print(f"  UNDEFINED  {path.name}: {', '.join(missing)}")
            problems.append(path.name)
        else:
            print(f"  ok         {path.name}")
    print()
    if problems:
        print(f"PROBLEMS IN: {', '.join(problems)}")
        return 1
    print("No undefined names.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
