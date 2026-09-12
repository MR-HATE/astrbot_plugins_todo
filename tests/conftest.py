"""测试公共装置：路径引导、假 Graph、假定时任务、假事件。

设计取舍（重要）：

- **不依赖 pytest**：本机没装 pytest，所以用例写成普通的 ``def test_*``，
  由 ``tests/run_tests.py`` 发现并执行；装了 pytest 也能直接 ``pytest tests/``。
- **不需要 pytest-asyncio**：异步逻辑在测试函数内部用 :func:`run` 驱动，
  这样同一份用例在两种运行器下都能跑。
- **包名从目录名推导**：插件目录可能叫 ``astrbot_plugin_todo``（与 metadata 一致）
  也可能叫 ``astrbot_plugins_todo``（历史仓库名）。用例统一 ``from plugin_pkg…``
  导入，conftest 负责把真实目录挂到 ``plugin_pkg`` 这个名字上，
  免得测试因为目录改名而整体跑不起来。
- 假 Graph / 假 cron 都是纯内存实现，并记录调用，便于断言"到底发了什么请求"。
"""

from __future__ import annotations

import asyncio
import importlib
import pathlib
import sys
from datetime import timedelta

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT.parent))

#: 插件目录名即包名；用别名 plugin_pkg 让用例与具体目录名解耦。
PLUGIN_PKG = REPO_ROOT.name
_plugin_package = importlib.import_module(PLUGIN_PKG)
sys.modules.setdefault("plugin_pkg", _plugin_package)
# 兼容直接用历史包名 import 的老写法
for _legacy in ("astrbot_plugin_todo", "astrbot_plugins_todo"):
    sys.modules.setdefault(_legacy, _plugin_package)

_planner_module = importlib.import_module("plugin_pkg.graph.planner")
local_today = _planner_module.local_today
TodoPlugin = importlib.import_module("plugin_pkg.main").TodoPlugin


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

#: 平台**实例 id**（AstrBot 配置里的 ``id``）。刻意与平台类型取不同值——
#: 这是为了防住"把 umo 第一段当成平台类型"这个真实踩过的坑：
#: 只有实例 id ≠ 类型时，写错的实现才会暴露。
PLATFORM_ID = "yume"
#: 平台**类型**（``PlatformMetadata.name``，如 aiocqhttp / telegram）
PLATFORM_TYPE = "aiocqhttp"
#: 真实 umo 形如 ``{实例id}:{MessageType}:{会话id}``
SESSION = f"{PLATFORM_ID}:FriendMessage:1747831170"
#: 会话所属用户
QQ_ID = "1747831170"


class FakeMeta:
    """对应 ``PlatformMetadata``：``name`` 是类型，``id`` 是实例 id。"""

    def __init__(
        self,
        name: str = PLATFORM_TYPE,
        platform_id: str = PLATFORM_ID,
        support_proactive_message: bool = True,
    ):
        self.name = name
        self.id = platform_id
        self.description = f"{name} (fake)"
        self.support_proactive_message = support_proactive_message


class FakePlatform:
    def __init__(self, meta: FakeMeta | None = None):
        self._meta = meta or FakeMeta()

    def meta(self) -> FakeMeta:
        return self._meta


class FakeEvent:
    def __init__(
        self,
        platform: str = PLATFORM_TYPE,
        sender: str = "10001",
        session: str | None = None,
        extras: dict | None = None,
        platform_id: str = PLATFORM_ID,
    ):
        self.platform = platform
        self.platform_id = platform_id
        self.sender = sender
        # 真实 umo 由「平台实例 id」拼成，不是平台类型
        self.unified_msg_origin = session or f"{platform_id}:FriendMessage:{sender}"
        self._extras: dict = dict(extras or {})

    def get_platform_name(self) -> str:
        return self.platform

    def get_sender_id(self) -> str:
        return self.sender

    # 与 AstrMessageEvent 对齐的 extras 接口（定时任务合成事件靠它传 cron_payload）
    def get_extra(self, key: str | None = None, default=None):
        if key is None:
            return self._extras
        return self._extras.get(key, default)

    def set_extra(self, key: str, value) -> None:
        self._extras[key] = value


def cron_event(session: str, sender: str, payload: dict | None = None) -> FakeEvent:
    """构造一个「定时任务唤起」的合成事件。

    真实实现里 ``CronMessageEvent`` 的 PlatformMetadata.name 被写死为 ``cron``，
    但 ``session`` 仍是原始会话——这个 helper 刻意复刻这一点，用来防"账号 key 算错"回归。
    """
    return FakeEvent(
        platform="cron",
        sender=sender,
        session=session,
        extras={"cron_payload": payload} if payload is not None else None,
    )


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


class _FakePlatformManager:
    def __init__(self, insts: list):
        self.platform_insts = insts


class FakeContext:
    """最小 Context：只提供插件真正用到的那几项。

    ``get_platform_inst`` 是"平台实例 id → 适配器实例"的查表口，
    插件靠它把实例 id 还原成平台类型。
    """

    def __init__(self, cron: FakeCron | None, platforms: dict | None = None):
        self.cron_manager = cron
        if platforms is None:
            platforms = {PLATFORM_ID: FakePlatform()}
        self._platforms = dict(platforms)

    def get_platform_inst(self, platform_id: str):
        return self._platforms.get(platform_id)

    @property
    def platform_manager(self) -> _FakePlatformManager:
        return _FakePlatformManager(list(self._platforms.values()))


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

    def __init__(
        self,
        *,
        bound=True,
        config=None,
        graph=None,
        cron=None,
        with_cron=True,
        platforms=None,
    ):
        self.store: dict = {}
        self.graph = graph if graph is not None else FakeGraph()
        self.cron = cron if cron is not None else FakeCron()
        self.plugin = TodoPlugin.__new__(TodoPlugin)
        self.plugin.config = {**DEFAULT_CONFIG, **(config or {})}
        self.plugin.graph = self.graph
        self.plugin.auth = FakeAuth(bound)
        self.plugin.context = FakeContext(self.cron if with_cron else None, platforms)
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
        # 走插件自己的 account_id，而不是重新拼「平台:发送者」——
        # 否则定时任务合成事件（平台名是 cron）这层逻辑就测不到了。
        return f"pending::{self.plugin.account_id(event)}"

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
