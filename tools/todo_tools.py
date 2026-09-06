"""M1 阶段提供的工具：账号绑定与连通性自检。

命名统一加 ``ms_todo_`` 前缀，避免与 AstrBot 内置工具（如 proactive agent 的
``future_task``）或其它插件的工具重名。
"""

from __future__ import annotations

from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.api import FunctionTool
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

_NO_PARAMS: dict = {"type": "object", "properties": {}}


@dataclass
class TodoToolBase(FunctionTool[AstrAgentContext]):
    """所有待办工具的基类。

    ``plugin`` 由插件在注册时注入（``ToolClass(plugin=self)``），
    工具通过它访问授权管理器与 Graph 客户端。
    """

    plugin: Any = None

    def _event(self, context: ContextWrapper[AstrAgentContext]):
        return context.context.event


@dataclass
class TodoAccountStatusTool(TodoToolBase):
    name: str = "ms_todo_account_status"
    description: str = (
        "查询当前用户是否已绑定 Microsoft To Do 账号，以及绑定的账号、token 有效期。"
        "当用户询问待办相关能力、或需要判断是否要先授权时调用。"
    )
    parameters: dict = Field(default_factory=lambda: dict(_NO_PARAMS))

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        return await self.plugin.tool_account_status(self._event(context))


@dataclass
class TodoLoginStartTool(TodoToolBase):
    name: str = "ms_todo_login_start"
    description: str = (
        "为当前用户发起 Microsoft To Do 账号绑定（OAuth 2.0 设备码授权）。"
        "返回一段包含授权网址和设备码的说明，必须把网址和代码原样转达给用户。"
        "当用户未绑定账号、或主动要求绑定/重新授权 Microsoft 待办时调用。"
    )
    parameters: dict = Field(default_factory=lambda: dict(_NO_PARAMS))

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        return await self.plugin.tool_login_start(self._event(context))


@dataclass
class TodoLogoutTool(TodoToolBase):
    name: str = "ms_todo_logout"
    description: str = (
        "解除当前用户与本机器人的 Microsoft To Do 绑定（删除本地保存的凭据）。"
        "仅在用户明确要求解绑/取消授权时调用。"
    )
    parameters: dict = Field(default_factory=lambda: dict(_NO_PARAMS))

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        return await self.plugin.tool_logout(self._event(context))


@dataclass
class TodoListTaskListsTool(TodoToolBase):
    name: str = "ms_todo_list_task_lists"
    description: str = (
        "列出当前用户 Microsoft To Do 中已有的待办列表（清单）。"
        "可用于确认连接是否正常，或在导入任务前让用户选择目标列表。"
    )
    parameters: dict = Field(default_factory=lambda: dict(_NO_PARAMS))

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        return await self.plugin.tool_list_task_lists(self._event(context))


#: 注册顺序即工具在 WebUI「函数工具管理」中的展示顺序。
TOOL_CLASSES: tuple[type[TodoToolBase], ...] = (
    TodoAccountStatusTool,
    TodoLoginStartTool,
    TodoLogoutTool,
    TodoListTaskListsTool,
)
