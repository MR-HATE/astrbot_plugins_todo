"""Microsoft Graph REST 薄封装。

只做四件事：
1. 自动附带 access_token（必要时先续期）；
2. 401 时强制刷新一次再重试；
3. 429/5xx 按 ``Retry-After`` 或指数退避重试；
4. 把 Graph 的错误体统一转成 ``GraphAPIError``（带 request-id，便于排查）。
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from astrbot.api import logger

from .auth import GRAPH_BASE, DeviceCodeAuth
from .errors import GraphAPIError

_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRY_AFTER = 60.0


def _safe_json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _parse_error(resp: httpx.Response) -> GraphAPIError:
    payload = _safe_json(resp)
    err = payload.get("error")
    code = "unknown"
    message = resp.text[:500] if resp.text else ""
    request_id = None
    if isinstance(err, dict):
        code = str(err.get("code") or code)
        message = str(err.get("message") or message)
        inner = err.get("innerError")
        if isinstance(inner, dict):
            request_id = inner.get("request-id") or inner.get("requestId")
    retry_after = None
    raw_retry = resp.headers.get("Retry-After")
    if raw_retry:
        try:
            retry_after = float(raw_retry)
        except ValueError:
            retry_after = None
    return GraphAPIError(
        resp.status_code,
        code,
        message,
        request_id=str(request_id) if request_id else None,
        retry_after=retry_after,
    )


class GraphClient:
    """带重试与自动续期的 Graph 调用封装。"""

    def __init__(
        self,
        auth: DeviceCodeAuth,
        http: httpx.AsyncClient,
        *,
        base: str = GRAPH_BASE,
        retries: int = 3,
    ) -> None:
        self._auth = auth
        self._http = http
        self._base = base.rstrip("/")
        self._retries = max(0, retries)

    async def request(
        self,
        aid: str,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict | None = None,
        retries: int | None = None,
    ) -> Any:
        """发起一次 Graph 请求，返回解析后的 JSON（204 返回 None）。"""
        url = path if path.startswith("http") else f"{self._base}/{path.lstrip('/')}"
        max_retries = self._retries if retries is None else max(0, retries)
        forced_refresh = False

        for attempt in range(max_retries + 1):
            token = await self._auth.get_access_token(aid)
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }
            try:
                resp = await self._http.request(
                    method, url, headers=headers, json=json, params=params
                )
            except httpx.HTTPError as exc:
                if attempt >= max_retries:
                    raise GraphAPIError(0, "network_error", f"网络请求失败：{exc!s}") from exc
                await asyncio.sleep(min(2**attempt, 8))
                continue

            if resp.status_code == 401 and not forced_refresh:
                # token 可能被服务端提前判定失效，强制续期后重试一次
                forced_refresh = True
                logger.info("graph 401, forcing token refresh for %s", aid)
                await self._force_refresh(aid)
                continue

            if resp.status_code in _RETRY_STATUS and attempt < max_retries:
                delay = _parse_error(resp).retry_after or min(2**attempt, 8)
                delay = min(float(delay), _MAX_RETRY_AFTER)
                logger.warning(
                    "graph %s on %s %s, retry in %.1fs (attempt %d/%d)",
                    resp.status_code,
                    method,
                    path,
                    delay,
                    attempt + 1,
                    max_retries,
                )
                await asyncio.sleep(delay)
                continue

            if resp.status_code >= 400:
                raise _parse_error(resp)

            if resp.status_code == 204 or not resp.content:
                return None
            try:
                return resp.json()
            except Exception:
                return None

        raise GraphAPIError(0, "retry_exhausted", "重试次数已用尽")

    async def _force_refresh(self, aid: str) -> None:
        """把本地凭据的过期时间提前，让下一次 get_access_token 触发续期。"""
        await self._auth.mark_access_token_expired(aid)

    async def get(self, aid: str, path: str, **kwargs) -> Any:
        return await self.request(aid, "GET", path, **kwargs)

    async def post(self, aid: str, path: str, **kwargs) -> Any:
        return await self.request(aid, "POST", path, **kwargs)

    async def patch(self, aid: str, path: str, **kwargs) -> Any:
        return await self.request(aid, "PATCH", path, **kwargs)

    async def delete(self, aid: str, path: str, **kwargs) -> Any:
        return await self.request(aid, "DELETE", path, **kwargs)

    async def get_me(self, aid: str) -> dict:
        data = await self.get(aid, "/me?$select=userPrincipalName,displayName,mail")
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------ 列表

    async def list_task_lists(self, aid: str) -> list[dict]:
        """列出用户的全部待办列表（自动跟分页，最多 5 页）。"""
        items: list[dict] = []
        path: str | None = "/me/todo/lists?$top=100"
        for _ in range(5):
            if not path:
                break
            data = await self.get(aid, path)
            if not isinstance(data, dict):
                break
            value = data.get("value")
            if isinstance(value, list):
                items.extend(item for item in value if isinstance(item, dict))
            next_link = data.get("@odata.nextLink")
            path = str(next_link) if next_link else None
        return items

    async def find_task_list(self, aid: str, name: str) -> dict | None:
        """按名称查找列表（忽略大小写与首尾空白）。"""
        wanted = (name or "").strip().casefold()
        if not wanted:
            return None
        for item in await self.list_task_lists(aid):
            display = str(item.get("displayName") or "").strip()
            if display.casefold() == wanted:
                return item
        return None

    async def create_task_list(self, aid: str, name: str) -> dict:
        data = await self.post(aid, "/me/todo/lists", json={"displayName": name})
        return data if isinstance(data, dict) else {}

    async def ensure_task_list(self, aid: str, name: str, *, create: bool = True) -> dict | None:
        """找到或创建目标列表。

        ``create=False`` 时找不到就返回 None，由上层回退到默认列表。
        """
        existing = await self.find_task_list(aid, name)
        if existing:
            return existing
        if not create:
            return None
        return await self.create_task_list(aid, name)

    # ------------------------------------------------------------------ 任务

    async def create_task(self, aid: str, list_id: str, payload: dict) -> dict:
        data = await self.post(aid, f"/me/todo/lists/{list_id}/tasks", json=payload)
        return data if isinstance(data, dict) else {}

    async def add_checklist_item(
        self, aid: str, list_id: str, task_id: str, display_name: str
    ) -> dict:
        data = await self.post(
            aid,
            f"/me/todo/lists/{list_id}/tasks/{task_id}/checklistItems",
            json={"displayName": display_name},
        )
        return data if isinstance(data, dict) else {}
