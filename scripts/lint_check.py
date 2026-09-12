"""轻量代码规范自检。

本机没有 ruff / pyflakes，所以提供这个零依赖替代品，覆盖最常出问题的几类：

1. 语法能否编译（``py_compile``）；
2. **未使用的 import**（AST 近似判断，``# noqa`` 的行会被跳过）；
3. 行宽超限（默认 100 列）、行尾空白、Tab 缩进、文件末尾缺换行；
4. 裸 ``except:``（E722）。

用法::

    python scripts/lint_check.py            # 检查仓库内主要目录
    python scripts/lint_check.py main.py    # 只检查指定文件/目录

装了 ruff 之后请以 ``ruff check .`` 为准。
"""

from __future__ import annotations

import ast
import pathlib
import py_compile
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_TARGETS = ("main.py", "graph", "tools", "tests", "scripts")
MAX_LINE = 100

#: 文件级豁免：在文件开头任意位置写 ``# lint: max-line=240`` 即可放宽该文件的行宽限制。
#: 用于「一张表 = 一行一条数据」这类拆行反而更难读的情况。
_MAX_LINE_DIRECTIVE = re.compile(r"#\s*lint:\s*max-line\s*=\s*(\d+)")


def _iter_files(targets: list[pathlib.Path]):
    for target in targets:
        if target.is_file() and target.suffix == ".py":
            yield target
        elif target.is_dir():
            for path in sorted(target.rglob("*.py")):
                if "__pycache__" not in path.parts:
                    yield path


def _collect_imports(tree: ast.AST) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "__future__":
                    continue
                found.append((alias.asname or alias.name.split(".")[0], node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] == "__future__":
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                found.append((alias.asname or alias.name, node.lineno))
    return found


def _collect_used_names(tree: ast.AST) -> set[str]:
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # __all__ = ["foo"] 之类的字符串导出
            used.add(node.value)
    return used


def check_file(path: pathlib.Path) -> list[str]:
    problems: list[str] = []
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return [f"{path}: 不是 UTF-8 文本（{exc}）"]

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [f"{path}:{exc.lineno}: 语法错误 {exc.msg}"]

    lines = source.splitlines()
    max_line = MAX_LINE
    directive = _MAX_LINE_DIRECTIVE.search(source)
    if directive:
        try:
            max_line = int(directive.group(1))
        except ValueError:
            pass

    for index, line in enumerate(lines, start=1):
        if "# noqa" in line:
            continue
        if len(line) > max_line:
            problems.append(f"{path}:{index}: 行宽 {len(line)} > {max_line}")
        if line.rstrip() != line:
            problems.append(f"{path}:{index}: 行尾有多余空白")
        if "\t" in line:
            problems.append(f"{path}:{index}: 使用了 Tab 缩进")

    if source and not source.endswith("\n"):
        problems.append(f"{path}: 文件末尾缺少换行")

    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            problems.append(f"{path}:{node.lineno}: 裸 except（应指明异常类型）")

    used = _collect_used_names(tree)
    for name, lineno in _collect_imports(tree):
        if name not in used:
            problems.append(f"{path}:{lineno}: 未使用的 import「{name}」")

    try:
        py_compile.compile(str(path), doraise=True, cfile=None)
    except py_compile.PyCompileError as exc:
        problems.append(f"{path}: 编译失败 {exc}")

    return problems


def main(argv: list[str]) -> int:
    raw_targets = argv[1:] or list(DEFAULT_TARGETS)
    targets = [
        pathlib.Path(item) if pathlib.Path(item).is_absolute() else REPO / item
        for item in raw_targets
    ]

    files = list(_iter_files(targets))
    if not files:
        print("没有找到要检查的 Python 文件")
        return 1

    all_problems: list[str] = []
    for path in files:
        all_problems.extend(check_file(path))

    for problem in all_problems:
        print(problem)

    print()
    print(f"检查了 {len(files)} 个文件，发现 {len(all_problems)} 个问题")
    print("（提示：装了 ruff 的话请以 `ruff check .` / `ruff format .` 为准）")
    return 1 if all_problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
