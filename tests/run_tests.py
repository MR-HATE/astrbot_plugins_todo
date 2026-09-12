"""零依赖测试运行器。

用法::

    python tests/run_tests.py          # 跑 tests/ 下所有 test_*.py
    python tests/run_tests.py planner  # 只跑文件名含 planner 的

本机没有 pytest，所以提供这个运行器；用例本身是普通的 ``def test_*``，
装了 pytest 后 ``pytest tests/`` 也能直接跑（无需 pytest-asyncio）。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import traceback

TESTS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
# 让 `astrbot_plugins_todo` 成为可导入的包（与 conftest.py 里的处理保持一致），
# 这样用例里可以写 `from astrbot_plugins_todo.graph.auth import ...`。
if str(REPO_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT.parent))


def _force_utf8_stdio() -> None:
    """Windows 控制台默认 GBK，输出 ✓/✗ 会直接 UnicodeEncodeError，这里兜一下。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _load_module(path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def main(argv: list[str]) -> int:
    _force_utf8_stdio()
    filters = [item for item in argv[1:] if not item.startswith("-")]
    files = sorted(
        path
        for path in TESTS_DIR.glob("test_*.py")
        if not filters or any(f in path.stem for f in filters)
    )
    if not files:
        print("没有找到匹配的测试文件")
        return 1

    passed = 0
    failures: list[tuple[str, str]] = []

    for path in files:
        module = _load_module(path)
        names = sorted(name for name in dir(module) if name.startswith("test_"))
        if not names:
            continue
        print(f"\n── {path.name} ──")
        for name in names:
            func = getattr(module, name)
            if not callable(func):
                continue
            try:
                func()
            except Exception:  # noqa: BLE001 - 运行器需要捕获一切并继续跑后面的用例
                failures.append((f"{path.name}::{name}", traceback.format_exc()))
                print(f"  ✗ {name}")
            else:
                passed += 1
                print(f"  ✓ {name}")

    print()
    for where, tb in failures:
        print(f"===== 失败：{where} =====")
        print(tb)
    total = passed + len(failures)
    print(f"共 {total} 个用例：通过 {passed}，失败 {len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
