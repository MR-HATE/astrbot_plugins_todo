"""AstrBot 插件：待办事项（Microsoft To Do 同步）。

M1 阶段完成的能力：
- OAuth 2.0 设备码授权（Device Code Flow），**每个用户各自绑定**，凭据按用户隔离；
- 聊天内指令：``/todo login`` / ``/todo status`` / ``/todo logout`` / ``/todo lists``；
- 配套 LLM 工具（函数调用），让模型可以主动引导用户完成绑定；
- 与 Microsoft Graph 的连通性自检。

后续阶段（见仓库根目录开发计划）会补齐「自然语言 → 待办清单 → 预览确认 → 写入
指定列表」的完整链路，以及 Skill 与定时提醒。
"""

from __future__ import annotations

from typing import Any

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain

from .graph import AccountStatus, DeviceCodeAuth, GraphClient, TodoError
from .tools import TOOL_CLASSES

PLUGIN_NAME = "astrbot_plugin_todo"
USER_AGENT = "astrbot-plugin-todo/0.1.0"


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
        """Microsoft To Do 待办：绑定账号、查看状态、查看列表"""

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
