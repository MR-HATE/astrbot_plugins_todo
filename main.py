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
    local_today,
    normalize_tasks,
    plan_expired,
    render_import_result,
    render_preview,
)
from .graph.models import clean_text
from .tools import TOOL_CLASSES

PLUGIN_NAME = "astrbot_plugin_todo"
USER_AGENT = "astrbot-plugin-todo/0.1.0"

#: 单次导入的并发写入数（To Do 接口很宽松，但没必要打满）
_IMPORT_CONCURRENCY = 3
#: 幂等记录保留条数上限（按列表维度）
_SEEN_MAX = 500

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

    @todo.command("import")
    async def todo_import(self, event: AstrMessageEvent, content: str = ""):
        """把一段自然语言计划整理成待办并生成预览，例：/todo import 下周去上海出差，周三前订酒店"""
        yield event.plain_result(await self.tool_import_from_text(event, content))

    @todo.command("confirm")
    async def todo_confirm(self, event: AstrMessageEvent):
        """确认并写入上一次生成的待办预览"""
        yield event.plain_result(await self._confirm_plan(self.account_id(event), ""))

    @todo.command("cancel")
    async def todo_cancel(self, event: AstrMessageEvent):
        """放弃上一次生成的待办预览（不会写入 Microsoft To Do）"""
        aid = self.account_id(event)
        plan = await self._load_pending(aid)
        if plan is None:
            yield event.plain_result("当前没有待确认的清单。")
            return
        await self._clear_pending(aid)
        yield event.plain_result(f"已放弃这次清单（{len(plan.tasks)} 项），没有写入 Microsoft To Do。")
