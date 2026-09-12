"""AstrBot 插件：待办事项（Microsoft To Do 同步）。

已完成的能力：
- OAuth 2.0 设备码授权（Device Code Flow），**每个用户各自绑定**，凭据按用户隔离；
- 聊天内指令：``/todo login|status|logout|lists|import|confirm|cancel``；
- 配套 LLM 工具（函数调用），让模型可以主动引导用户绑定并导入待办；
- 自然语言 → 结构化待办 → **预览确认** → 按计划名建列表 → 写入 Microsoft To Do。

后续阶段（见仓库根目录开发计划）会补齐 Skill 说明书与 AstrBot 定时提醒。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from typing import Any

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain

from .graph import (
    PENDING_TTL,
    AccountStatus,
    DeviceCodeAuth,
    GraphClient,
    PlanDraft,
    TaskDraft,
    TodoError,
    apply_seen,
    dedupe_batch,
    filter_tasks,
    local_today,
    normalize_tasks,
    plan_expired,
    render_import_result,
    render_preview,
    render_task_list,
    sort_tasks,
)
from .graph.models import NOTE_MAX, TITLE_MAX, clean_text
from .graph.planner import (
    TASK_STATUS_ICONS,
    format_due,
    normalize_important,
    resolve_date,
    resolve_time,
    task_due,
)
from .tools import TOOL_CLASSES

PLUGIN_NAME = "astrbot_plugin_todo"
USER_AGENT = "astrbot-plugin-todo/0.1.0"

#: 单次导入的并发写入数（To Do 接口很宽松，但没必要打满）
_IMPORT_CONCURRENCY = 3
#: 幂等记录保留条数上限（按列表维度）
_SEEN_MAX = 500
#: 查询待办时最多同时读取多少个列表（防止列表特别多时打爆接口）
_MAX_LISTS_PER_QUERY = 10
#: 查询范围白名单
_VALID_SCOPES = frozenset({"pending", "today", "overdue", "upcoming", "completed", "all"})

#: 注入标记：同一次请求的工具循环会复用同一个 ProviderRequest，用它保证只追加一次
_RULES_MARKER = "<!-- astrbot-plugin-todo:rules -->"

#: 追加到 system prompt 的待办规则（内容固定，不影响提示词缓存）。
#:
#: 为什么需要它？AstrBot 的 Skills 机制只在提示词里内联 name + description，
#: SKILL.md 正文要 Agent 自己用 shell 读取——没开「使用电脑能力」或普通用户执行命令被拒时
#: 就读不到。这里把最关键的几条规则兜底注入，保证任何环境都生效。
_TODO_RULES_PROMPT = f"""{_RULES_MARKER}
# Microsoft To Do 待办（astrbot_plugin_todo）

当用户要把事情记成待办、或提到待办清单时，使用 `ms_todo_*` 工具，并遵守：

1. **先预览、后写入**：调用 `ms_todo_import_tasks` 时 `confirmed` 一律留空，把返回的预览
   原样发给用户；用户确认后再调用 `ms_todo_confirm_import(action="confirm")` 写入。
   没有用户确认就不要写。
2. **不编造日期**：没有明确时间就不填 `due_date`；「月底」「下个月」这类无法确定具体日期的
   说法留空，并向用户说明。
3. **拆分粒度**：一件能独立完成的事 = 一个任务；同一件事的多个步骤放进 `steps`，
   不要把一个动作拆成多个任务。
4. **别误触发**：闲聊、普通提问不要建任务；用户意图不明时先追问，不要臆造待办。
5. **如实回报**：预览里的「⚠️ 需要留意」必须转达用户；工具报错就说失败，不要假装成功。
6. **读取待办用 `ms_todo_list_tasks`**：用户问「今天还有什么要做的」「上周的事做完没」时用它，
   它返回**真实的完成状态**（⬜ 未完成 / 🔄 进行中 / ✅ 已完成 / ⏳ 等待他人 / 💤 已推迟）；
   默认范围是「未完成且今天到期或已逾期」。这是只读操作。
7. **改动已有待办**：改期/改优先级/改标题用 `ms_todo_update_task`；勾完成用
   `ms_todo_complete_task`；加子步骤用 `ms_todo_add_checklist_items`。
   定位不到唯一一条时，工具会返回候选清单——**要让用户选，不要自己猜**。
8. **删除是两阶段**：`ms_todo_delete_task`（删一条）和 `ms_todo_delete_list`（删整个列表，
   连同里面的任务）第一次都只返回「将删除什么」，把清单给用户看，用户确认后再用
   `confirmed=true` 调用一次。删除不可恢复，绝不要跳过一次确认。"""

_EXTRACT_SYSTEM_PROMPT = """你是一个待办抽取器。把用户的话拆成结构化的待办事项。

今天是 {today}（时区 {tz}，星期{weekday}）。

只输出一个 JSON 对象，不要解释、不要 markdown 代码块，格式：
{{
  "plan_name": "这次计划的简短名称，例如 上海出差",
  "tasks": [
    {{
      "title": "待办标题（动作描述）",
      "due_date": "YYYY-MM-DD 或 null",
      "due_time": "HH:MM 或 null",
      "importance": "low|normal|high",
      "note": "补充说明或 null",
      "remind_at": "YYYY-MM-DDTHH:MM 或 null",
      "steps": ["子步骤"]
    }}
  ]
}}

规则：
1. 一件可独立完成的事 = 一个 task；一件事的多个步骤放进 steps，不要把一件事拆成十个任务。
2. 所有相对时间（明天、下周三、3 天后）必须换算成 YYYY-MM-DD 绝对日期；没有时间信息就填 null，不要猜。
3. 用户说「重要/紧急」用 high，说「不急」用 low，其余 normal。
4. 用户明确要求提醒时才填 remind_at。
5. 只抽取真正的待办，不要臆造用户没说的内容。"""


def _extract_json(text: str) -> Any:
    """从模型输出里抠出 JSON（容忍 ```json 代码块与前后废话）。"""
    if not text:
        return None
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # 退而求其次：截取第一个 { 到最后一个 }
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _build_http_client(config: dict) -> httpx.AsyncClient:
    """按配置创建共享的 httpx 异步客户端（官方规范：不要用 requests）。"""
    try:
        timeout = float(config.get("request_timeout") or 30)
    except (TypeError, ValueError):
        timeout = 30.0
    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(timeout),
        "headers": {"User-Agent": USER_AGENT},
    }
    proxy = str(config.get("proxy") or "").strip()
    if not proxy:
        return httpx.AsyncClient(**kwargs)
    try:
        # httpx >= 0.26 使用 proxy=
        return httpx.AsyncClient(proxy=proxy, **kwargs)
    except TypeError:
        # httpx < 0.26 使用 proxies=
        return httpx.AsyncClient(proxies=proxy, **kwargs)


@register(
    PLUGIN_NAME,
    "Mr_Hate",
    "把「接下来要办的事」整理成待办清单，并导入 Microsoft To Do。",
    "v0.1.0",
)
class TodoPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        # 注意：Star.__init__ 不会保存 config，必须自己存一份
        self.config: dict = config if config is not None else {}
        self._http = _build_http_client(self.config)
        self.auth = DeviceCodeAuth(
            star=self,
            http=self._http,
            config=lambda: dict(self.config),
            on_login=self._on_login_success,
        )
        self.graph = GraphClient(self.auth, self._http)
        self._register_tools()

    # ------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        """插件实例化后由 AstrBot 调用。"""
        if not self._cfg("client_id", ""):
            logger.warning(
                "未配置 client_id，Microsoft To Do 相关功能不可用。"
                "请在插件配置中填写 Entra 应用的应用程序(客户端) ID。"
            )
        else:
            logger.info(
                "astrbot_plugin_todo 已就绪（tenant=%s, 时区=%s）",
                self._cfg("tenant", "common"),
                self._cfg("timezone", "Asia/Shanghai"),
            )

    async def terminate(self) -> None:
        """插件被禁用/重载时清理后台任务与 HTTP 连接。"""
        try:
            await self.auth.close()
        except Exception as exc:  # noqa: BLE001 - 卸载路径不应抛异常
            logger.warning("关闭授权管理器失败: %s", exc)
        try:
            await self._http.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.warning("关闭 HTTP 客户端失败: %s", exc)

    # ------------------------------------------------------------ 工具注册

    def _register_tools(self) -> None:
        tools = [tool_cls(plugin=self) for tool_cls in TOOL_CLASSES]
        self.context.add_llm_tools(*tools)
        # AstrBot 的 add_llm_tools 在部分部署形态下会把 handler_module_path 算成
        # 不含 data.plugins. 前缀的路径，导致插件重载后工具被静默置为 inactive
        # （AstrBotDevs/AstrBot#8578）。这里直接对齐 star_manager 使用的插件主模块路径。
        module_path = self.__class__.__module__
        for tool in tools:
            tool.handler_module_path = module_path
        logger.info("已注册 %d 个 LLM 工具: %s", len(tools), ", ".join(t.name for t in tools))

    # ------------------------------------------------------------ 配置工具

    def _cfg(self, key: str, default: Any = None) -> Any:
        getter = getattr(self.config, "get", None)
        if not callable(getter):
            return default
        value = getter(key, default)
        return default if value is None else value

    @staticmethod
    def account_id(event: AstrMessageEvent) -> str:
        """账号标识：按「平台 + 用户」隔离，保证群聊里每人各自一份凭据。"""
        return f"{event.get_platform_name()}:{event.get_sender_id()}"

    def _check_allowed(self, event: AstrMessageEvent) -> str | None:
        """返回 None 表示允许使用；否则返回拒绝文案。"""
        if bool(self._cfg("allow_any_user", True)):
            return None
        allowed = self._cfg("allowed_users", []) or []
        sender = str(event.get_sender_id())
        if sender in {str(item) for item in allowed}:
            return None
        return "本插件的待办功能已限制使用范围，请联系管理员将你的用户 ID 加入白名单。"

    # ------------------------------------------------- 业务逻辑（指令/工具共用）

    async def tool_account_status(self, event: AstrMessageEvent) -> str:
        aid = self.account_id(event)
        try:
            status = await self.auth.status(aid)
        except TodoError as exc:
            return exc.user_message
        return status.to_text()

    async def tool_login_start(self, event: AstrMessageEvent) -> str:
        denied = self._check_allowed(event)
        if denied:
            return denied
        aid = self.account_id(event)
        try:
            challenge = await self.auth.start(aid, session=event.unified_msg_origin)
        except TodoError as exc:
            return exc.user_message
        except Exception as exc:  # noqa: BLE001 - 兜底，避免工具抛异常中断 Agent
            logger.exception("发起设备码授权失败")
            return f"发起授权失败：{exc!s}"
        return challenge.to_text()

    async def tool_logout(self, event: AstrMessageEvent) -> str:
        aid = self.account_id(event)
        status = await self.auth.status(aid)
        if not status.bound and not status.pending:
            return "当前没有已绑定的 Microsoft 账号，无需解绑。"
        await self.auth.logout(aid)
        return "已解除绑定，并删除本机保存的凭据。重新使用请发送 /todo login。"

    async def tool_list_task_lists(self, event: AstrMessageEvent) -> str:
        aid = self.account_id(event)
        try:
            lists = await self.graph.list_task_lists(aid)
        except TodoError as exc:
            return exc.user_message
        except Exception as exc:  # noqa: BLE001
            logger.exception("获取待办列表失败")
            return f"获取待办列表失败：{exc!s}"
        if not lists:
            return "连接正常，但你的 Microsoft To Do 中还没有任何列表。"
        lines = ["连接正常，当前 Microsoft To Do 列表："]
        for item in lists:
            name = item.get("displayName") or "(未命名)"
            wellknown = item.get("wellknownListName")
            suffix = "（默认列表）" if wellknown == "defaultList" else ""
            lines.append(f"- {name}{suffix}  `{item.get('id')}`")
        return "\n".join(lines)

    # -------------------------------------------------------- 查询待办（M4）

    @staticmethod
    def _normalize_scope(raw: object) -> str:
        scope = clean_text(raw, 16).lower()
        return scope if scope in _VALID_SCOPES else "pending"

    async def _target_lists(self, aid: str, list_name: str) -> list[dict] | None:
        """确定要查询的列表；指定了不存在的列表时返回 None。"""
        if list_name:
            found = await self.graph.find_task_list(aid, list_name)
            return [found] if found else None
        return await self.graph.list_task_lists(aid)

    async def _collect_tasks(
        self, aid: str, lists: list[dict]
    ) -> tuple[list[dict], list[str]]:
        """并发读取多个列表的任务，返回 (任务列表, 错误说明)。"""
        semaphore = asyncio.Semaphore(_IMPORT_CONCURRENCY)
        collected: list[dict] = []
        errors: list[str] = []

        async def fetch(item: dict) -> None:
            list_id = str(item.get("id") or "")
            if not list_id:
                return
            async with semaphore:
                try:
                    tasks = await self.graph.list_tasks(aid, list_id)
                except TodoError as exc:
                    errors.append(f"{item.get('displayName')}：{exc.user_message}")
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.exception("读取待办失败")
                    errors.append(f"{item.get('displayName')}：{exc!s}")
                    return
            name = str(item.get("displayName") or "")
            for task in tasks:
                task["_list_name"] = name  # 汇总展示时标明来源列表
            collected.extend(tasks)

        await asyncio.gather(*(fetch(item) for item in lists[:_MAX_LISTS_PER_QUERY]))
        return collected, errors

    async def tool_list_tasks(self, event: AstrMessageEvent, args: dict) -> str:
        """读取待办及其完成状态（只读）。"""
        denied = self._check_allowed(event)
        if denied:
            return denied

        aid = self.account_id(event)
        status = await self.auth.status(aid)
        if not status.bound:
            return "还没有绑定 Microsoft 账号。请先发送 /todo login 完成绑定。"

        args = args or {}
        scope = self._normalize_scope(args.get("scope"))
        list_name = clean_text(args.get("list_name"), 100)
        try:
            limit = int(args.get("limit") or 30)
        except (TypeError, ValueError):
            limit = 30
        limit = max(1, min(limit, 100))

        tz = self._tz()
        try:
            lists = await self._target_lists(aid, list_name)
        except TodoError as exc:
            return exc.user_message
        except Exception as exc:  # noqa: BLE001
            logger.exception("获取待办列表失败")
            return f"获取待办列表失败：{exc!s}"

        if lists is None:
            return f"没找到名为「{list_name}」的列表。发送 /todo lists 看看有哪些列表。"
        if not lists:
            return "你的 Microsoft To Do 里还没有任何列表。"

        collected, errors = await self._collect_tasks(aid, lists)
        tasks = sort_tasks(filter_tasks(collected, scope, tz))

        label = f"列表「{list_name}」" if list_name else "全部列表"
        note = ""
        if errors:
            note = "⚠️ 部分列表读取失败：" + "；".join(errors[:3])
        if len(lists) > _MAX_LISTS_PER_QUERY:
            more = f"（列表较多，本次只读取了前 {_MAX_LISTS_PER_QUERY} 个）"
            note = f"{note}\n{more}".strip() if note else more

        return render_task_list(
            tasks, scope=scope, list_label=label, tz_name=tz, max_show=limit, note=note
        )

    # -------------------------------------------------------- 增删改（M4）

    @staticmethod
    def _pending_delete_key(aid: str) -> str:
        return f"pending_delete::{aid}"

    async def _save_pending_delete(
        self, aid: str, entries: list[dict], *, kind: str = "task"
    ) -> None:
        await self.put_kv_data(
            self._pending_delete_key(aid),
            {"kind": kind, "items": entries, "created_at": time.time()},
        )

    async def _load_pending_delete(self, aid: str) -> dict | None:
        data = await self.get_kv_data(self._pending_delete_key(aid), None)
        return dict(data) if isinstance(data, dict) else None

    async def _locate_tasks(
        self, aid: str, args: dict
    ) -> tuple[list[tuple[dict, dict]], str | None]:
        """定位要操作的任务，返回 ([(列表, 任务)], 错误文案)。"""
        task_id = clean_text(args.get("task_id"), 300)
        query = clean_text(args.get("query"), 100)
        list_name = clean_text(args.get("list_name"), 100)

        if not task_id and not query:
            return [], "需要提供 query（标题关键词）或 task_id 才能定位任务。"

        try:
            lists = await self._target_lists(aid, list_name)
        except TodoError as exc:
            return [], exc.user_message
        if lists is None:
            return [], f"没找到名为「{list_name}」的列表。发送 /todo lists 看看有哪些列表。"
        if not lists:
            return [], "你的 Microsoft To Do 里还没有任何列表。"

        matches: list[tuple[dict, dict]] = []
        needle = query.casefold()

        for item in lists[:_MAX_LISTS_PER_QUERY]:
            list_id = str(item.get("id") or "")
            if not list_id:
                continue
            try:
                tasks = await self.graph.list_tasks(aid, list_id)
            except TodoError as exc:
                return [], exc.user_message
            except Exception as exc:  # noqa: BLE001
                logger.exception("读取待办失败")
                return [], f"读取待办失败：{exc!s}"

            for task in tasks:
                if task_id:
                    if str(task.get("id")) != task_id:
                        continue
                elif needle and needle not in str(task.get("title") or "").casefold():
                    continue
                task["_list_id"] = list_id
                task["_list_name"] = str(item.get("displayName") or "")
                matches.append((item, task))

        if task_id:
            if not matches:
                return [], "没有找到该 task_id 对应的任务（可能已被删除）。"
            return matches, None

        # 标题完全相等的排最前，方便"就是它"的情况直接命中
        matches.sort(
            key=lambda pair: 0
            if str(pair[1].get("title") or "").strip().casefold() == needle
            else 1
        )
        return matches, None

    @staticmethod
    def _render_task_matches(matches: list[tuple[dict, dict]], tz_name: str) -> str:
        """匹配到多条时，列出候选让用户/模型挑一个。"""
        today = local_today(tz_name)
        lines = [f"找到 {len(matches)} 个匹配的待办，需要先确定是哪一个："]
        for index, (_item, task) in enumerate(matches[:10], start=1):
            icon = TASK_STATUS_ICONS.get(str(task.get("status") or ""), "⬜")
            due_date, due_time = task_due(task)
            details = []
            if due_date:
                details.append(format_due(due_date, due_time, today))
            if task.get("_list_name"):
                details.append(f"列表「{task['_list_name']}」")
            suffix = f"（{' · '.join(details)}）" if details else ""
            lines.append(f"{index}. {icon} {task.get('title')}{suffix}  id={task.get('id')}")
        lines.append("请用户确认是哪一个，然后用它的 id 或更精确的标题重新调用。")
        return "\n".join(lines)

    def _resolve_one(
        self, matches: list[tuple[dict, dict]], tz_name: str
    ) -> tuple[dict | None, str | None]:
        if not matches:
            return None, "没有找到匹配的待办。可以先用 ms_todo_list_tasks 查一下准确的标题。"
        if len(matches) > 1:
            return None, self._render_task_matches(matches, tz_name)
        return matches[0][1], None

    def _build_task_update(
        self, args: dict, task: dict, tz_name: str
    ) -> tuple[dict, list[str]]:
        """根据工具参数拼 PATCH 请求体，返回 (payload, 提示)。"""
        today = local_today(tz_name)
        payload: dict = {}
        notes: list[str] = []

        title = clean_text(args.get("title"), TITLE_MAX)
        if title:
            payload["title"] = title

        if "importance" in args and str(args.get("importance") or "").strip():
            importance, warning = normalize_important(args.get("importance"))
            payload["importance"] = importance
            if warning:
                notes.append(warning)

        if "status" in args and str(args.get("status") or "").strip():
            status = clean_text(args.get("status"), 32)
            if status in TASK_STATUS_ICONS:
                payload["status"] = status
            else:
                notes.append(f"状态「{status}」不在允许范围内，已忽略")

        if "note" in args and args.get("note") is not None:
            payload["body"] = {
                "content": clean_text(args.get("note"), NOTE_MAX),
                "contentType": "text",
            }

        if args.get("clear_due"):
            payload["dueDateTime"] = None
        elif str(args.get("due_date") or "").strip():
            day, warning = resolve_date(args.get("due_date"), today)
            if not day:
                notes.append(warning or "无法识别新的截止日期，已忽略")
            else:
                clock = None
                if str(args.get("due_time") or "").strip():
                    clock, time_warning = resolve_time(args.get("due_time"))
                    if time_warning:
                        notes.append(time_warning)
                if not clock:
                    _existing_date, existing_time = task_due(task)
                    clock = existing_time or "00:00"
                payload["dueDateTime"] = {
                    "dateTime": f"{day}T{clock}:00",
                    "timeZone": tz_name,
                }
        elif str(args.get("due_time") or "").strip():
            # 只改时刻：保留原日期
            existing_date, _existing_time = task_due(task)
            clock, warning = resolve_time(args.get("due_time"))
            if existing_date and clock:
                payload["dueDateTime"] = {
                    "dateTime": f"{existing_date}T{clock}:00",
                    "timeZone": tz_name,
                }
            elif warning:
                notes.append(warning)

        return payload, notes

    async def tool_update_task(self, event: AstrMessageEvent, args: dict) -> str:
        denied = self._check_allowed(event)
        if denied:
            return denied
        aid = self.account_id(event)
        status = await self.auth.status(aid)
        if not status.bound:
            return "还没有绑定 Microsoft 账号。请先发送 /todo login 完成绑定。"

        args = args or {}
        tz = self._tz()
        matches, error = await self._locate_tasks(aid, args)
        if error:
            return error
        task, problem = self._resolve_one(matches, tz)
        if problem:
            return problem
        assert task is not None

        payload, notes = self._build_task_update(args, task, tz)
        if not payload:
            return "没有识别出要修改的内容。请说明改什么（标题/截止时间/优先级/备注）。"

        list_id = str(task.get("_list_id") or "")
        task_id = str(task.get("id") or "")
        try:
            await self.graph.update_task(aid, list_id, task_id, payload)
        except TodoError as exc:
            return exc.user_message
        except Exception as exc:  # noqa: BLE001
            logger.exception("修改待办失败")
            return f"修改失败：{exc!s}"

        changed = []
        if "title" in payload:
            changed.append(f"标题改为「{payload['title']}」")
        if "dueDateTime" in payload:
            if payload["dueDateTime"] is None:
                changed.append("清除截止时间")
            else:
                stamp = str(payload["dueDateTime"]["dateTime"])
                changed.append(f"截止改为 {stamp[:10]} {stamp[11:16]}")
        if "importance" in payload:
            changed.append(f"优先级改为 {payload['importance']}")
        if "status" in payload:
            changed.append(f"状态改为 {payload['status']}")
        if "body" in payload:
            changed.append("备注已更新")

        lines = [f"✅ 已修改「{task.get('title')}」：", "- " + "；".join(changed)]
        if notes:
            lines.append("提示：" + "；".join(notes))
        return "\n".join(lines)

    async def tool_complete_task(self, event: AstrMessageEvent, args: dict) -> str:
        denied = self._check_allowed(event)
        if denied:
            return denied
        aid = self.account_id(event)
        status = await self.auth.status(aid)
        if not status.bound:
            return "还没有绑定 Microsoft 账号。请先发送 /todo login 完成绑定。"

        args = args or {}
        tz = self._tz()
        matches, error = await self._locate_tasks(aid, args)
        if error:
            return error
        task, problem = self._resolve_one(matches, tz)
        if problem:
            return problem
        assert task is not None

        completed = args.get("completed")
        completed = True if completed is None else bool(completed)
        new_status = "completed" if completed else "notStarted"

        try:
            await self.graph.update_task(
                aid,
                str(task.get("_list_id") or ""),
                str(task.get("id") or ""),
                {"status": new_status},
            )
        except TodoError as exc:
            return exc.user_message
        except Exception as exc:  # noqa: BLE001
            logger.exception("更新完成状态失败")
            return f"更新失败：{exc!s}"

        verb = "已完成" if completed else "已重新打开"
        return f"{'✅' if completed else '⬜'} 「{task.get('title')}」{verb}。"

    async def tool_delete_task(self, event: AstrMessageEvent, args: dict) -> str:
        denied = self._check_allowed(event)
        if denied:
            return denied
        aid = self.account_id(event)
        status = await self.auth.status(aid)
        if not status.bound:
            return "还没有绑定 Microsoft 账号。请先发送 /todo login 完成绑定。"

        args = args or {}
        if bool(args.get("confirmed")):
            return await self._execute_pending_delete(aid)

        tz = self._tz()
        matches, error = await self._locate_tasks(aid, args)
        if error:
            return error
        task, problem = self._resolve_one(matches, tz)
        if problem:
            return problem
        assert task is not None

        entry = {
            "task_id": str(task.get("id") or ""),
            "list_id": str(task.get("_list_id") or ""),
            "title": str(task.get("title") or ""),
            "list_name": str(task.get("_list_name") or ""),
        }
        await self._save_pending_delete(aid, [entry])

        due_date, due_time = task_due(task)
        detail = f"（截止 {format_due(due_date, due_time, local_today(tz))}）" if due_date else ""
        return (
            f"⚠️ 即将删除 1 项，删除后无法恢复：\n"
            f"- {entry['title']}{detail} · 列表「{entry['list_name']}」\n"
            '请用户确认；确认后调用 ms_todo_delete_task(confirmed=true) 才会真正删除。'
        )

    async def tool_delete_list(self, event: AstrMessageEvent, args: dict) -> str:
        """删除整个列表（连同其中的任务），两阶段确认。"""
        denied = self._check_allowed(event)
        if denied:
            return denied
        aid = self.account_id(event)
        status = await self.auth.status(aid)
        if not status.bound:
            return "还没有绑定 Microsoft 账号。请先发送 /todo login 完成绑定。"

        args = args or {}
        if bool(args.get("confirmed")):
            return await self._execute_pending_delete(aid)

        list_name = clean_text(args.get("list_name") or args.get("query"), 100)
        if not list_name:
            return "请说明要删除哪个列表。可以先用 ms_todo_list_task_lists 看看有哪些列表。"

        try:
            lists = await self.graph.list_task_lists(aid)
        except TodoError as exc:
            return exc.user_message
        except Exception as exc:  # noqa: BLE001
            logger.exception("获取待办列表失败")
            return f"获取待办列表失败：{exc!s}"

        wanted = list_name.casefold()
        matched = [item for item in lists if str(item.get("displayName") or "").strip().casefold() == wanted]
        if not matched:
            partial = [item for item in lists if wanted in str(item.get("displayName") or "").casefold()]
            if len(partial) == 1:
                matched = partial
            elif len(partial) > 1:
                names = "、".join(f"「{item.get('displayName')}」" for item in partial[:10])
                return f"找到多个相近的列表：{names}。请说明要删哪一个，或用 /todo lists 查看完整列表。"
            else:
                return f"没找到名为「{list_name}」的列表。发送 /todo lists 看看有哪些列表。"

        target = matched[0]
        if str(target.get("wellknownListName") or "") == "defaultList":
            return (
                f"「{target.get('displayName')}」是 Microsoft To Do 的默认列表，"
                "客户端不允许删除它。可以先把里面的任务清掉，或用别的列表。"
            )

        list_id = str(target.get("id") or "")
        display = str(target.get("displayName") or list_name)
        try:
            tasks = await self.graph.list_tasks(aid, list_id)
        except Exception as exc:  # noqa: BLE001 - 数不出来也不该阻止确认流程
            logger.warning("统计列表任务数失败: %s", exc)
            tasks = []
        pending_count = sum(
            1 for task in tasks if str(task.get("status") or "") != "completed"
        )

        await self._save_pending_delete(
            aid,
            [{"list_id": list_id, "list_name": display, "task_count": len(tasks)}],
            kind="list",
        )

        detail = f"该列表下有 {len(tasks)} 个任务" if tasks else "该列表下没有任务"
        if pending_count:
            detail += f"（其中 {pending_count} 个未完成）"
        return (
            f"⚠️ 即将删除整个列表「{display}」，{detail}。\n"
            "列表和里面的任务会一起消失，且无法恢复。\n"
            '请用户确认；确认后调用 ms_todo_delete_list(confirmed=true) 才会真正删除。'
        )

    async def _execute_pending_delete(self, aid: str) -> str:
        pending = await self._load_pending_delete(aid)
        if not pending:
            return "没有待确认的删除操作。请先说明要删哪一项。"
        if plan_expired(float(pending.get("created_at") or 0), now=time.time()):
            await self.delete_kv_data(self._pending_delete_key(aid))
            return f"上次的删除确认已超过 {PENDING_TTL // 60} 分钟，为安全起见没有执行。请重新确认要删哪一项。"

        items = [item for item in (pending.get("items") or []) if isinstance(item, dict)]
        kind = str(pending.get("kind") or "task")
        deleted: list[str] = []
        failed: list[str] = []

        if kind == "list":
            for item in items:
                list_id = str(item.get("list_id") or "")
                name = str(item.get("list_name") or "")
                try:
                    await self.graph.delete_task_list(aid, list_id)
                except TodoError as exc:
                    failed.append(f"{name}：{exc.user_message}")
                except Exception as exc:  # noqa: BLE001
                    logger.exception("删除列表失败")
                    failed.append(f"{name}：{exc!s}")
                else:
                    deleted.append(name)
                    # 列表没了，它的幂等记录也没有意义
                    await self.delete_kv_data(self._seen_key(list_id))
            await self.delete_kv_data(self._pending_delete_key(aid))
            lines = (
                [f"🗑 已删除列表：" + "、".join(f"「{name}」" for name in deleted)]
                if deleted
                else ["没有删除任何列表。"]
            )
            if failed:
                lines.append("❌ 失败：" + "；".join(failed))
            return "\n".join(lines)

        for item in items:
            try:
                await self.graph.delete_task(
                    aid, str(item.get("list_id") or ""), str(item.get("task_id") or "")
                )
            except TodoError as exc:
                failed.append(f"{item.get('title')}：{exc.user_message}")
            except Exception as exc:  # noqa: BLE001
                logger.exception("删除待办失败")
                failed.append(f"{item.get('title')}：{exc!s}")
            else:
                deleted.append(str(item.get("title") or ""))
                # 顺手清掉幂等记录，避免以后同一条被当成"已导入过"
                await self._forget_seen(
                    aid, str(item.get("list_id") or ""), str(item.get("task_id") or "")
                )

        await self.delete_kv_data(self._pending_delete_key(aid))
        lines = [f"🗑 已删除 {len(deleted)} 项：" + "、".join(deleted)] if deleted else ["没有删除任何待办。"]
        if failed:
            lines.append("❌ 失败：" + "；".join(failed))
        return "\n".join(lines)

    async def _forget_seen(self, aid: str, list_id: str, task_id: str) -> None:
        """删除任务后同步清掉幂等表里的对应记录。"""
        if not list_id or not task_id:
            return
        seen = await self._load_seen(list_id)
        removed = [key for key, value in seen.items() if str((value or {}).get("task_id")) == task_id]
        if not removed:
            return
        for key in removed:
            seen.pop(key, None)
        await self._save_seen(list_id, seen)

    async def tool_add_checklist_items(self, event: AstrMessageEvent, args: dict) -> str:
        denied = self._check_allowed(event)
        if denied:
            return denied
        aid = self.account_id(event)
        status = await self.auth.status(aid)
        if not status.bound:
            return "还没有绑定 Microsoft 账号。请先发送 /todo login 完成绑定。"

        args = args or {}
        items = [clean_text(item, 250) for item in (args.get("items") or []) if item]
        items = [item for item in items if item]
        if not items:
            return "没有收到要添加的子步骤。"

        tz = self._tz()
        matches, error = await self._locate_tasks(aid, args)
        if error:
            return error
        task, problem = self._resolve_one(matches, tz)
        if problem:
            return problem
        assert task is not None

        list_id = str(task.get("_list_id") or "")
        task_id = str(task.get("id") or "")
        added: list[str] = []
        failed: list[str] = []
        for text in items:
            try:
                await self.graph.add_checklist_item(aid, list_id, task_id, text)
            except Exception as exc:  # noqa: BLE001
                logger.warning("添加子步骤失败: %s", exc)
                failed.append(text)
            else:
                added.append(text)

        lines = []
        if added:
            lines.append(f"✅ 已给「{task.get('title')}」添加 {len(added)} 个子步骤：" + " / ".join(added))
        if failed:
            lines.append("❌ 以下子步骤添加失败：" + " / ".join(failed))
        return "\n".join(lines) or "没有添加任何子步骤。"

    # -------------------------------------------------------- 导入链路（M2）

    def _tz(self) -> str:
        return str(self._cfg("timezone", "Asia/Shanghai"))

    @staticmethod
    def _pending_key(aid: str) -> str:
        return f"pending::{aid}"

    @staticmethod
    def _seen_key(list_id: str) -> str:
        return f"seen::{list_id}"

    async def _load_pending(self, aid: str) -> PlanDraft | None:
        data = await self.get_kv_data(self._pending_key(aid), None)
        if not isinstance(data, dict):
            return None
        try:
            return PlanDraft.from_dict(data)
        except Exception as exc:  # noqa: BLE001 - 脏数据不该让功能不可用
            logger.warning("读取暂存计划失败: %s", exc)
            return None

    async def _save_pending(self, plan: PlanDraft) -> None:
        await self.put_kv_data(self._pending_key(plan.aid), plan.to_dict())

    async def _clear_pending(self, aid: str) -> None:
        await self.delete_kv_data(self._pending_key(aid))

    async def _load_seen(self, list_id: str) -> dict:
        data = await self.get_kv_data(self._seen_key(list_id), {})
        return dict(data) if isinstance(data, dict) else {}

    async def _save_seen(self, list_id: str, seen: dict) -> None:
        if len(seen) > _SEEN_MAX:
            ordered = sorted(
                seen.items(),
                key=lambda kv: float((kv[1] or {}).get("created_at") or 0),
                reverse=True,
            )
            seen = dict(ordered[:_SEEN_MAX])
        await self.put_kv_data(self._seen_key(list_id), seen)

    def _pick_list_name(self, args: dict) -> tuple[str, bool]:
        """决定目标列表名，返回 (名称, 是否用户显式指定)。"""
        explicit = clean_text(args.get("list_name"), 100)
        plan_name = clean_text(args.get("plan_name"), 100)
        default_name = clean_text(self._cfg("default_list", "AstrBot 待办"), 100) or "AstrBot 待办"
        if explicit:
            return explicit, True
        if plan_name and bool(self._cfg("auto_create_list", True)):
            return plan_name, False
        return default_name, False

    async def _resolve_target_list(
        self, aid: str, wanted: str, *, explicit: bool = True
    ) -> tuple[dict, str]:
        """找到或创建目标列表，返回 (列表对象, 给用户的说明)。"""
        auto_create = bool(self._cfg("auto_create_list", True))
        default_name = clean_text(self._cfg("default_list", "AstrBot 待办"), 100) or "AstrBot 待办"
        name = clean_text(wanted, 100) or default_name

        existing = await self.graph.find_task_list(aid, name)
        if existing:
            return existing, ""

        # 默认列表无论如何都要保证存在；其它列表是否自动新建由 auto_create_list 决定
        if auto_create or name == default_name:
            created = await self.graph.create_task_list(aid, name)
            if created.get("id"):
                return created, f"（已新建列表「{name}」）"

        if name != default_name:
            fallback = await self.graph.ensure_task_list(aid, default_name, create=True)
            if fallback and fallback.get("id"):
                reason = "未找到" if explicit else "未启用自动建列表"
                return fallback, f"（{reason}列表「{name}」，已改用「{fallback.get('displayName')}」）"
        return {}, ""

    async def tool_import_tasks(self, event: AstrMessageEvent, args: dict) -> str:
        """导入链路主入口（工具与指令共用）。"""
        denied = self._check_allowed(event)
        if denied:
            return denied

        aid = self.account_id(event)
        args = args or {}
        confirmed = bool(args.get("confirmed"))
        plan_id = clean_text(args.get("plan_id"), 64)
        raw_tasks = args.get("tasks") or []

        # 情况一：只带 plan_id（或什么都没有）——走"确认既有计划"
        if not raw_tasks:
            if not plan_id:
                return "没有收到待办内容。请先把用户说的计划整理成 tasks 数组再调用。"
            return await self._confirm_plan(aid, plan_id)

        # 未绑定就直说，避免用户确认完才发现写不进去
        status = await self.auth.status(aid)
        if not status.bound:
            return "还没有绑定 Microsoft 账号，无法导入。请先发送 /todo login 完成绑定。"

        tz = self._tz()
        drafts = normalize_tasks(raw_tasks, tz)
        if not drafts:
            return "没有解析出有效的待办（可能是每项都缺少 title）。请确认 tasks 里每项都有标题。"

        cap = int(self._cfg("max_tasks_per_import", 20) or 20)
        truncated = 0
        if cap > 0 and len(drafts) > cap:
            truncated = len(drafts) - cap
            drafts = drafts[:cap]

        list_name, _explicit = self._pick_list_name(args)
        plan = PlanDraft(
            plan_id=uuid.uuid4().hex[:8],
            aid=aid,
            session=event.unified_msg_origin,
            list_name=list_name,
            tasks=drafts,
            created_at=time.time(),
        )

        if confirmed and plan_id:
            # 模型带回了预览里的 plan_id：以用户看过的那份计划为准
            return await self._confirm_plan(aid, plan_id)

        require_confirm = bool(self._cfg("require_confirm", True))
        if confirmed and not require_confirm:
            # 用户关掉了"写入前确认"，直接落库
            return await self._execute_plan(plan)

        await self._save_pending(plan)
        preview = render_preview(list_name, drafts, plan.plan_id, tz)
        if truncated:
            preview += f"\n\n（超出单次上限 {cap} 项，本次只包含前 {cap} 项，另有 {truncated} 项未纳入）"
        if require_confirm:
            preview += (
                "\n\n下一步：把上面的预览原样发给用户并等待确认；"
                '用户确认后调用 ms_todo_confirm_import(action="confirm")。'
            )
        else:
            preview += (
                "\n\n（当前配置为免确认模式，可直接调用 "
                'ms_todo_confirm_import(action="confirm") 写入）'
            )
        return preview

    async def tool_confirm_import(self, event: AstrMessageEvent, args: dict) -> str:
        aid = self.account_id(event)
        action = clean_text((args or {}).get("action"), 16).lower()
        plan_id = clean_text((args or {}).get("plan_id"), 64)

        if action == "cancel":
            plan = await self._load_pending(aid)
            if plan is None:
                return "当前没有待确认的清单。"
            await self._clear_pending(aid)
            return f"已放弃这次清单（{len(plan.tasks)} 项），没有写入 Microsoft To Do。"
        if action == "confirm":
            return await self._confirm_plan(aid, plan_id)
        return 'action 必须是 "confirm" 或 "cancel"。'

    async def _confirm_plan(self, aid: str, plan_id: str) -> str:
        plan = await self._load_pending(aid)
        if plan is None:
            return "没有找到待确认的清单（可能已经导入、已取消或已过期）。请重新整理一次。"
        if plan_id and plan.plan_id != plan_id:
            return (
                f"plan_id 不匹配：当前待确认的是 {plan.plan_id}，你给的是 {plan_id}。"
                "请重新生成预览后再确认。"
            )
        if plan_expired(plan.created_at, now=time.time()):
            await self._clear_pending(aid)
            return (
                f"上次的清单已经超过 {PENDING_TTL // 60} 分钟，为避免误写我没有导入。"
                "请重新说一遍要办的事。"
            )
        try:
            return await self._execute_plan(plan)
        except TodoError as exc:
            return exc.user_message
        except Exception as exc:  # noqa: BLE001 - 工具不应抛异常打断 Agent
            logger.exception("导入待办失败")
            return f"导入失败：{exc!s}"

    async def _execute_plan(self, plan: PlanDraft) -> str:
        aid = plan.aid
        tz = self._tz()

        target, note = await self._resolve_target_list(aid, plan.list_name)
        list_id = str(target.get("id") or "")
        list_name = str(target.get("displayName") or plan.list_name)
        if not list_id:
            return "无法确定目标列表（Microsoft 未返回列表 ID），本次导入已取消。"

        unique, duplicated = dedupe_batch(plan.tasks, list_name)
        seen = await self._load_seen(list_id)
        fresh, skipped = apply_seen(unique, list_name, seen)

        created: list[tuple[TaskDraft, str | None]] = []
        recorded: list[tuple[str, str, str]] = []
        failed: list[tuple[TaskDraft, str]] = []
        semaphore = asyncio.Semaphore(_IMPORT_CONCURRENCY)

        async def create_one(task: TaskDraft) -> None:
            key = task.source_key or task.fingerprint(list_name)
            async with semaphore:
                try:
                    data = await self.graph.create_task(aid, list_id, task.to_graph_task(tz))
                except TodoError as exc:
                    failed.append((task, exc.user_message))
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.exception("创建待办失败")
                    failed.append((task, str(exc)))
                    return

            task_id = str(data.get("id") or "")
            created.append((task, task_id or None))
            recorded.append((key, task_id, task.title))

            # 子步骤失败不影响主任务
            for step in task.steps:
                try:
                    await self.graph.add_checklist_item(aid, list_id, task_id, step)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("写入子步骤失败（task=%s）: %s", task_id, exc)

        await asyncio.gather(*(create_one(task) for task in fresh))

        now = time.time()
        for key, task_id, title in recorded:
            seen[key] = {
                "task_id": task_id,
                "title": title,
                "list": list_name,
                "created_at": now,
            }
        if recorded:
            await self._save_seen(list_id, seen)
        await self._clear_pending(aid)

        text = render_import_result(
            list_name, created, skipped + duplicated, failed, tz_name=tz
        )
        if note:
            text += f"\n{note}"
        return text

    async def tool_import_from_text(self, event: AstrMessageEvent, content: str) -> str:
        """指令兜底通道：不依赖模型函数调用，直接用 LLM 抽取再走同一条导入链路。"""
        content = (content or "").strip()
        if not content:
            return (
                "请在 /todo import 后面接上你的计划，例如：\n"
                "/todo import 下周去上海出差，周三前订好酒店，出发前一天提醒我带身份证"
            )
        denied = self._check_allowed(event)
        if denied:
            return denied

        aid = self.account_id(event)
        status = await self.auth.status(aid)
        if not status.bound:
            return "还没有绑定 Microsoft 账号。请先发送 /todo login 完成绑定。"

        try:
            provider_id = await self.context.get_current_chat_provider_id(
                umo=event.unified_msg_origin
            )
        except Exception as exc:  # noqa: BLE001
            return f"没有可用的对话模型，无法解析计划：{exc!s}"

        tz = self._tz()
        today = local_today(tz)
        system_prompt = _EXTRACT_SYSTEM_PROMPT.format(
            today=today.isoformat(),
            tz=tz,
            weekday="一二三四五六日"[today.weekday()],
        )
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=content,
                system_prompt=system_prompt,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("调用模型抽取待办失败")
            return f"解析计划失败：{exc!s}"

        data = _extract_json(getattr(resp, "completion_text", "") or "")
        if isinstance(data, list):
            data = {"tasks": data, "plan_name": ""}
        if not isinstance(data, dict) or not data.get("tasks"):
            return "没能从这段话里解析出待办。可以换个说法，或把要做的事说得具体一点。"

        return await self.tool_import_tasks(
            event,
            {"tasks": data.get("tasks"), "plan_name": data.get("plan_name") or ""},
        )

    # ------------------------------------------------------------ 通知

    async def _on_login_success(self, aid: str, status: AccountStatus) -> None:
        """授权完成后主动通知用户。"""
        session = status.session
        if not session:
            return
        who = status.display_name or ""
        upn = f" <{status.upn}>" if status.upn else ""
        text = (
            f"✅ Microsoft To Do 绑定成功：{who}{upn}\n"
            "现在可以直接对我说「帮我记一下接下来的计划」。"
        )
        try:
            await self.context.send_message(session, MessageChain().message(text))
        except Exception as exc:  # noqa: BLE001 - 通知失败不影响授权结果
            logger.warning("发送绑定成功通知失败: %s", exc)

    # ------------------------------------------------------------ LLM 钩子

    @filter.on_llm_request()
    async def inject_todo_rules(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """把待办规则追加到 system prompt。

        钩子里不能 yield，也不建议追加每轮变化的内容（会破坏服务端提示词缓存）；
        这里注入的是固定文本，且用标记保证同一次请求只追加一次。
        """
        if not bool(self._cfg("inject_rules", True)):
            return
        current = getattr(req, "system_prompt", "") or ""
        if _RULES_MARKER in current:
            return
        req.system_prompt = f"{current}\n{_TODO_RULES_PROMPT}\n" if current else _TODO_RULES_PROMPT

    # ------------------------------------------------------------ 指令

    @filter.command_group("todo")
    def todo(self):
        """Microsoft To Do 待办：绑定账号、导入计划、查看列表"""

    @todo.command("login")
    async def todo_login(self, event: AstrMessageEvent):
        """绑定 / 重新绑定 Microsoft 账号（设备码授权，无需在服务器上开浏览器）"""
        yield event.plain_result(await self.tool_login_start(event))

    @todo.command("status")
    async def todo_status(self, event: AstrMessageEvent):
        """查看当前账号的绑定状态与凭据有效期"""
        yield event.plain_result(await self.tool_account_status(event))

    @todo.command("logout")
    async def todo_logout(self, event: AstrMessageEvent):
        """解除绑定并删除本机保存的凭据"""
        yield event.plain_result(await self.tool_logout(event))

    @todo.command("lists")
    async def todo_lists(self, event: AstrMessageEvent):
        """列出 Microsoft To Do 中已有的列表（同时用于验证连通性）"""
        yield event.plain_result(await self.tool_list_task_lists(event))

    @todo.command("today")
    async def todo_today(self, event: AstrMessageEvent):
        """查看今天还没做完的待办（含已逾期的）"""
        yield event.plain_result(await self.tool_list_tasks(event, {"scope": "pending"}))

    @todo.command("done")
    async def todo_done(self, event: AstrMessageEvent, content: str = ""):
        """把匹配的待办标记为已完成，例：/todo done 交房租"""
        yield event.plain_result(await self.tool_complete_task(event, {"query": content}))

    @todo.command("del")
    async def todo_del(self, event: AstrMessageEvent, content: str = ""):
        """删除一条待办（需再发一次 /todo confirm 确认），例：/todo del 买菜"""
        yield event.plain_result(await self.tool_delete_task(event, {"query": content}))

    @todo.command("dellist")
    async def todo_dellist(self, event: AstrMessageEvent, content: str = ""):
        """删除整个列表及其中的任务（需再发一次 /todo confirm 确认），例：/todo dellist 上海出差"""
        yield event.plain_result(await self.tool_delete_list(event, {"list_name": content}))

    @todo.command("import")
    async def todo_import(self, event: AstrMessageEvent, content: str = ""):
        """把一段自然语言计划整理成待办并生成预览，例：/todo import 下周去上海出差，周三前订酒店"""
        yield event.plain_result(await self.tool_import_from_text(event, content))

    @todo.command("confirm")
    async def todo_confirm(self, event: AstrMessageEvent):
        """确认上一次的待办预览；若刚发起过删除，则确认执行删除"""
        aid = self.account_id(event)
        if await self._load_pending_delete(aid):
            yield event.plain_result(await self._execute_pending_delete(aid))
            return
        yield event.plain_result(await self._confirm_plan(aid, ""))

    @todo.command("cancel")
    async def todo_cancel(self, event: AstrMessageEvent):
        """放弃上一次生成的待办预览（不会写入 Microsoft To Do）"""
        aid = self.account_id(event)
        plan = await self._load_pending(aid)
        if plan is None:
            if await self._load_pending_delete(aid):
                await self.delete_kv_data(self._pending_delete_key(aid))
                yield event.plain_result("已取消这次删除，没有删除任何待办。")
                return
            yield event.plain_result("当前没有待确认的清单或删除操作。")
            return
        await self._clear_pending(aid)
        yield event.plain_result(f"已放弃这次清单（{len(plan.tasks)} 项），没有写入 Microsoft To Do。")
