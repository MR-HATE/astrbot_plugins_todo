"""基于 AstrBot 插件 KV 的账号凭据存储。

为什么要按「账号」而不是按「会话」存？
    用户要求「每个用户各自绑定」。群聊里 ``unified_msg_origin`` 是整个群的，
    用它做 key 会导致同群用户互相覆盖 token。因此这里以
    ``{platform_name}:{sender_id}`` 作为账号 id（aid），并在记录里额外保存
    最近一次交互的会话（session），用于主动推送通知。
"""

from __future__ import annotations

from typing import Any

_INDEX_KEY = "accounts"
_ACCOUNT_PREFIX = "account::"


def account_key(aid: str) -> str:
    """账号 id → KV key。"""
    return f"{_ACCOUNT_PREFIX}{aid}"


class AccountStore:
    """把 OAuth 凭据写进 AstrBot 的插件级 KV（shared_preferences）。

    AstrBot 通过 ``plugin_id``（``{author}/{name}``，均为小写）对插件存储做命名空间
    隔离，因此这里直接复用 ``Star.put_kv_data`` 系列方法，不自己碰文件。
    """

    def __init__(self, star: Any) -> None:
        self._star = star

    async def load(self, aid: str) -> dict | None:
        data = await self._star.get_kv_data(account_key(aid), None)
        return dict(data) if isinstance(data, dict) else None

    async def save(self, aid: str, record: dict) -> None:
        await self._star.put_kv_data(account_key(aid), record)
        index = await self.list_accounts()
        if aid not in index:
            index.append(aid)
            await self._star.put_kv_data(_INDEX_KEY, index)

    async def delete(self, aid: str) -> None:
        await self._star.delete_kv_data(account_key(aid))
        index = await self.list_accounts()
        if aid in index:
            index.remove(aid)
            await self._star.put_kv_data(_INDEX_KEY, index)

    async def list_accounts(self) -> list[str]:
        """返回已绑定过的账号 id 列表（仅用于统计 / WebUI 展示）。"""
        data = await self._star.get_kv_data(_INDEX_KEY, [])
        if not isinstance(data, list):
            return []
        return [str(item) for item in data]
