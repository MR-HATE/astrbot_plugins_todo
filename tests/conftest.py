"""测试公共装置：路径引导、假 Graph、假定时任务、假事件。

设计取舍（重要）：

- **不依赖 pytest**：本机没装 pytest，所以用例写成普通的 ``def test_*``，
  由 ``tests/run_tests.py`` 发现并执行；装了 pytest 也能直接 ``pytest tests/``。
- **不需要 pytest-asyncio**：异步逻辑在测试函数内部用 :func:`run` 驱动，
  这样同一份用例在两种运行器下都能跑。
- 假 Graph / 假 cron 都是纯内存实现，并记录调用，便于断言"到底发了什么请求"。
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
from datetime import timedelta

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT.parent) not in sys.path:  # 让 astrbot_plugins_todo 成为可导入的包
    sys.path.insert(0, str(REPO_ROOT.parent))

from astrbot_plugins_todo.graph.planner import local_today  # noqa: E402
from astrbot_plugins_todo.main import TodoPlugin  # noqa: E402


def run(coro):
    """同步驱动一个协程（用例里统一用它，避免依赖 pytest-asyncio）。"""
    return asyncio.run(coro)


def today():
    return local_today("Asia/Shanghai")


def days(offset: int) -> str:
    """相对今天的 YYYY-MM-DD。"""
    return (today() + timedelta(days=offset)).isoformat()


def at(offset_days: int, clock: str) -> str:
    """相对今天的 YYYY-MM-DDTHH:MM。"""
    return f"{days(offset_days)}T{clock}"


# ---------------------------------------------------------------- 假 Graph


class GraphAPIError(RuntimeError):
    pass


class FakeGraph:
    """内存版 Graph 客户端，接口与 GraphClient 对齐并记录调用。"""

    def __init__(self, lists: dict[str, str] | None = None):
        self.lists: dict[str, dict] = {}
        self.tasks: dict[str, list[dict]] = {}
        self.calls: list[tuple] = []
        self.fail_titles: set[str] = set()
        self.fail_batch = False
        self._seq = 0
        for name, list_id in (lists or {"生活": "L1"}).items():
            self.lists[name] = {"id": list_id, "displayName": name}
            self.tasks[list_id] = []

    # --- 列表 ---
    def add_list(self, name: str, list_id: str | None = None, **extra) -> dict:
        list_id = list_id or f"L{len(self.lists) + 1}"
        item = {"id": list_id, "displayName": name, **extra}
        self.lists[name] = item
        self.tasks.setdefault(list_id, [])
        return item

    def add_task(self, list_id: str, title: str, **fields) -> dict:
        self._seq += 1
        task = {"id": f"t{self._seq}", "title": title, "status": "notStarted", **fields}
        self.tasks.setdefault(list_id, []).append(task)
        return task

    async def list_task_lists(self, aid: str) -> list[dict]:
        return [dict(item) for item in self.lists.values()]

    async def find_task_list(self, aid: str, name: str) -> dict | None:
        return self.lists.get((name or "").strip())

    async def create_task_list(self, aid: str, name: str) -> dict:
        self.calls.append(("create_list", name))
        return self.add_list(name)

    async def ensure_task_list(self, aid: str, name: str, *, create: bool = True) -> dict | None:
        found = await self.find_task_list(aid, name)
        if found or not create:
            return found
        return await self.create_task_list(aid, name)

    async def delete_task_list(self, aid: str, list_id: str) -> None:
        self.calls.append(("delete_list", list_id))
        self.tasks.pop(list_id, None)
        self.lists = {k: v for k, v in self.lists.items() if v["id"] != list_id}

    # --- 任务 ---
    async def list_tasks(self, aid: str, list_id: str, **kwargs) -> list[dict]:
        return [dict(t) for t in self.tasks.get(list_id, [])]

    def _create(self, list_id: str, payload: dict) -> dict:
        title = payload.get("title", "")
        if title in self.fail_titles:
            raise GraphAPIError(f"模拟失败: {title}")
        self._seq += 1
        task = {"id": f"t{self._seq}", "status": "notStarted", **payload}
        self.tasks.setdefault(list_id, []).append(task)
        return task

    async def create_task(self, aid: str, list_id: str, payload: dict) -> dict:
        self.calls.append(("create_task", list_id, payload.get("title")))
        return self._create(list_id, payload)

    async def batch_create_tasks(self, aid: str, list_id: str, payloads: list[dict]) -> list[dict]:
        self.calls.append(("batch", list_id, [p.get("title") for p in payloads]))
        results = []
        for payload in payloads:
            try:
                task = self._create(list_id, payload)
            except GraphAPIError as exc:
                results.append({"status": 400, "task": None, "error": str(exc)})
            else:
                results.append({"status": 201, "task": task, "error": None})
        return results

    async def update_task(self, aid: str, list_id: str, task_id: str, payload: dict) -> dict:
        self.calls.append(("update_task", task_id, payload))
        for task in self.tasks.get(list_id, []):
            if task["id"] == task_id:
                task.update(payload)
        return {"id": task_id}

    async def delete_task(self, aid: str, list_id: str, task_id: str) -> None:
        self.calls.append(("delete_task", task_id))
        self.tasks[list_id] = [t for t in self.tasks.get(list_id, []) if t["id"] != task_id]

    async def add_checklist_item(self, aid: str, list_id: str, task_id: str, name: str) -> dict:
        self.calls.append(("checklist", task_id, name))
        return {"id": f"c{len(self.calls)}", "displayName": name}


# ---------------------------------------------------------------- 假定时任务


class FakeCronJob:
    def __init__(self, job_id: str):
        self.job_id = job_id


class FakeCron:
    """内存版 CronJobManager，记录 add_active_job 的完整参数。"""

    def __init__(self, available: bool = True):
        self.available = available
        self.jobs: list[dict] = []
        self.deleted: list[str] = []
        self.raise_on_add: Exception | None = None

    async def add_active_job(self, **kwargs) -> FakeCronJob:
        if self.raise_on_add is not None:
            raise self.raise_on_add
        self.jobs.append(kwargs)
        return FakeCronJob(f"job{len(self.jobs)}")

    async def delete_job(self, job_id: str) -> None:
        self.deleted.append(job_id)


# ---------------------------------------------------------------- 假事件/鉴权


class FakeEvent:
    def __init__(
        self,
        platform: str = "aiocqhttp",
        sender: str = "10001",
        session: str | None = None,
    ):
        self.platform = platform
        self.sender = sender
        self.unified_msg_origin = session or f"{platform}:private:{sender}"

    def get_platform_name(self) -> str:
        return self.platform

    def get_sender_id(self) -> str:
        return self.sender


class FakeStatus:
    def __init__(self, bound: bool):
        self.bound = bound
        self.pending = False


class FakeAuth:
    def __init__(self, bound: bool = True):
        self.bound = bound
        self.started: list[tuple] = []

    async def status(self, aid: str) -> FakeStatus:
        return FakeStatus(self.bound)

    async def start(self, aid: str, *, session: str):
        self.started.append((aid, session))
        raise AssertionError("测试中不应真的发起授权")


class FakeContext:
    def __init__(self, cron: FakeCron | None):
        self.cron_manager = cron


# ---------------------------------------------------------------- 插件工厂

DEFAULT_CONFIG = {
    "timezone": "Asia/Shanghai",
    "default_list": "AstrBot 待办",
    "auto_create_list": True,
    "require_confirm": True,
    "max_tasks_per_import": 20,
    "reminder_enabled": True,
    "inject_rules": True,
    "allow_any_user": True,
}


class PluginHarness:
    """把插件实例、假 Graph、假 cron、内存 KV 打包，方便测试取用。"""

    def __init__(self, *, bound=True, config=None, graph=None, cron=None, with_cron=True):
        self.store: dict = {}
        self.graph = graph if graph is not None else FakeGraph()
        self.cron = cron if cron is not None else FakeCron()
        self.plugin = TodoPlugin.__new__(TodoPlugin)
        self.plugin.config = {**DEFAULT_CONFIG, **(config or {})}
        self.plugin.graph = self.graph
        self.plugin.auth = FakeAuth(bound)
        self.plugin.context = FakeContext(self.cron if with_cron else None)
        self.plugin.get_kv_data = self._kv_get
        self.plugin.put_kv_data = self._kv_put
        self.plugin.delete_kv_data = self._kv_del

    async def _kv_get(self, key, default=None):
        return self.store.get(key, default)

    async def _kv_put(self, key, value):
        self.store[key] = value

    async def _kv_del(self, key):
        self.store.pop(key, None)

    # --- 常用动作 ---
    def event(self, **kwargs) -> FakeEvent:
        return FakeEvent(**kwargs)

    def pending_key(self, event: FakeEvent) -> str:
        return f"pending::{event.get_platform_name()}:{event.get_sender_id()}"

    # 用例都是同步函数，这里统一用 run() 驱动插件的异步工具方法，
    # 免得每个断言点都写一遍 asyncio.run。
    def stage(self, event: FakeEvent, args: dict) -> str:
        """阶段一：生成预览。"""
        return run(self.plugin.tool_import_tasks(event, args))

    def confirm(self, event: FakeEvent) -> str:
        """阶段二：确认写入（自动读取暂存计划）。"""
        plan_id = self.store[self.pending_key(event)]["plan_id"]
        return run(
            self.plugin.tool_confirm_import(event, {"action": "confirm", "plan_id": plan_id})
        )

    def confirm_import(self, event: FakeEvent, args: dict) -> str:
        return run(self.plugin.tool_confirm_import(event, args))

    def import_now(self, event: FakeEvent, tasks, plan_name="测试") -> str:
        self.stage(event, {"tasks": tasks, "plan_name": plan_name})
        return self.confirm(event)

    def update_task(self, event: FakeEvent, args: dict) -> str:
        return run(self.plugin.tool_update_task(event, args))

    def complete_task(self, event: FakeEvent, args: dict) -> str:
        return run(self.plugin.tool_complete_task(event, args))

    def delete_task(self, event: FakeEvent, args: dict) -> str:
        return run(self.plugin.tool_delete_task(event, args))

    def delete_list(self, event: FakeEvent, args: dict) -> str:
        return run(self.plugin.tool_delete_list(event, args))

    def add_checklist(self, event: FakeEvent, args: dict) -> str:
        return run(self.plugin.tool_add_checklist_items(event, args))

    def list_tasks(self, event: FakeEvent, args: dict) -> str:
        return run(self.plugin.tool_list_tasks(event, args))

    def list_lists(self, event: FakeEvent) -> str:
        return run(self.plugin.tool_list_task_lists(event))

    def status(self, event: FakeEvent) -> str:
        return run(self.plugin.tool_account_status(event))


def make_harness(**kwargs) -> PluginHarness:
    return PluginHarness(**kwargs)
