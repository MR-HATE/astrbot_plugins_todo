"""插件内统一的异常类型。

设计原则：所有可预期的失败都抛 ``TodoError`` 的子类，并携带**可以直接说给用户听**的
中文提示（``user_message``），这样工具层与指令层不需要各自拼装错误文案。
"""

from __future__ import annotations


class TodoError(Exception):
    """插件内所有可预期错误的基类。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    @property
    def user_message(self) -> str:
        """适合直接回复给用户的文案。"""
        return self.message


class ConfigError(TodoError):
    """插件配置缺失或非法（例如未填写 client_id）。"""

    @property
    def user_message(self) -> str:
        return f"插件配置有误：{self.message}"


class AuthRequiredError(TodoError):
    """当前账号未绑定 Microsoft 账号，或授权已失效，需要重新授权。"""

    @property
    def user_message(self) -> str:
        return f"{self.message}（发送 /todo login 重新授权）"


class AuthFlowError(TodoError):
    """设备码授权流程本身的失败（被拒绝、超时、验证码失效、网络异常等）。"""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code

    @property
    def user_message(self) -> str:
        return f"Microsoft 授权失败：{self.message}"


class GraphAPIError(TodoError):
    """Microsoft Graph 返回了 4xx/5xx。"""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        request_id: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.request_id = request_id
        self.retry_after = retry_after

    @property
    def is_throttled(self) -> bool:
        return self.status == 429

    @property
    def user_message(self) -> str:
        if self.status == 401:
            return "Microsoft 授权已过期，请重新执行 /todo login"
        if self.status == 403:
            return (
                "Microsoft 拒绝了本次操作（403）。请确认账号类型受支持，"
                "且已授予 Tasks.ReadWrite 权限；世纪互联（21Vianet）版账号不支持 To Do API。"
            )
        if self.is_throttled:
            return "Microsoft 接口限流中，请稍后再试。"
        if self.status == 404:
            return f"目标不存在或已被删除（404）：{self.message}"
        return f"Microsoft 接口返回错误（HTTP {self.status}, {self.code}）：{self.message}"

    def __str__(self) -> str:
        rid = f", request-id={self.request_id}" if self.request_id else ""
        return f"GraphAPIError(status={self.status}, code={self.code}{rid}): {self.message}"
