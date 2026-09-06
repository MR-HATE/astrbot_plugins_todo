"""Microsoft To Do（Graph API）接入层。

模块划分：
- ``errors``:  插件内统一的异常类型
- ``store``:   基于 AstrBot 插件 KV 的凭据存储
- ``auth``:    OAuth 2.0 Device Code Flow（含 refresh token 自动续期）
- ``client``:  Microsoft Graph REST 薄封装（重试 / 限流 / 错误分类）
"""

from .auth import AccountStatus, DeviceCodeAuth, DeviceCodeChallenge
from .client import GraphClient
from .errors import (
    AuthFlowError,
    AuthRequiredError,
    ConfigError,
    GraphAPIError,
    TodoError,
)
from .store import AccountStore

__all__ = [
    "AccountStatus",
    "AccountStore",
    "AuthFlowError",
    "AuthRequiredError",
    "ConfigError",
    "DeviceCodeAuth",
    "DeviceCodeChallenge",
    "GraphAPIError",
    "GraphClient",
    "TodoError",
]
