"""``graph/auth.py`` 的配置解析用例。

只覆盖"配置怎么读、缺配置怎么报错"这类纯函数行为；
涉及网络的设备码轮询与 token 续期不在单元测试范围内（见 ``tests/manual-checklist.md``）。
"""

from __future__ import annotations

from plugin_pkg.graph.auth import DEFAULT_SCOPE, DeviceCodeAuth
from plugin_pkg.graph.errors import ConfigError


def _auth(config: dict) -> DeviceCodeAuth:
    """绕过 __init__，只构造出 _client_id/_tenant/_scope 需要的最小状态。"""
    auth = DeviceCodeAuth.__new__(DeviceCodeAuth)
    auth._config = lambda: config
    return auth


def test_missing_client_id_raises_config_error():
    """缺 client_id 时必须抛 ConfigError（上层会转成给用户看的中文提示）。"""
    for config in ({}, {"client_id": ""}, {"client_id": "   "}, {"client_id": None}):
        try:
            _auth(config)._client_id()
        except ConfigError:
            continue
        raise AssertionError(f"配置 {config!r} 应当抛 ConfigError")


def test_explicit_client_id_wins():
    assert _auth({"client_id": "my-own-app-id"})._client_id() == "my-own-app-id"
    # 前后空白要被裁掉，避免用户从门户复制时带空格
    assert _auth({"client_id": "  abc-123  "})._client_id() == "abc-123"


def test_tenant_defaults_to_common():
    assert _auth({})._tenant() == "common"
    assert _auth({"tenant": "   "})._tenant() == "common"
    assert _auth({"tenant": "consumers"})._tenant() == "consumers"


def test_scope_defaults():
    assert _auth({})._scope() == DEFAULT_SCOPE
    assert _auth({"scope": "  "})._scope() == DEFAULT_SCOPE
    assert _auth({"scope": "Tasks.ReadWrite"})._scope() == "Tasks.ReadWrite"


def test_default_scope_covers_required_permissions():
    """offline_access 是拿到 refresh_token 的前提，Tasks.ReadWrite 是 To Do 的最小权限。"""
    assert "offline_access" in DEFAULT_SCOPE
    assert "Tasks.ReadWrite" in DEFAULT_SCOPE


def test_client_secret_optional():
    """公共客户端（设备码流程推荐用法）没有 secret。"""
    assert _auth({})._client_secret() == ""
    assert _auth({"client_secret": "s3cret"})._client_secret() == "s3cret"
