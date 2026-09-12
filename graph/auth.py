"""OAuth 2.0 设备码授权（Device Code Flow）实现。

为什么用设备码流程？
    Microsoft To Do 的 Graph API **只支持委派权限（Delegated）**，不支持
    Application 权限，所以无法用 client_credentials 无人值守写入。设备码流程是
    官方为「无浏览器设备」设计的方案：AstrBot 只需出网请求，用户在**自己的**
    手机/电脑浏览器上完成登录，无需回调地址、无需内网穿透、无需在服务器上装浏览器。

凭据生命周期：
    - access_token 默认 1 小时，过期前自动用 refresh_token 静默续期；
    - refresh_token 官方默认 90 天，每次刷新都会返回新的 refresh_token，
      这里每次都覆盖保存（旧的直接丢弃）；
    - 一旦刷新失败（invalid_grant 等），删除本地凭据并抛 ``AuthRequiredError``，
      由上层引导用户重新 ``/todo login``。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from astrbot.api import logger

from .errors import AuthFlowError, AuthRequiredError, ConfigError, GraphAPIError
from .store import AccountStore

AUTHORITY_TMPL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

#: offline_access 是拿到 refresh_token 的前提，Tasks.ReadWrite 是 To Do 的最小权限。
DEFAULT_SCOPE = "offline_access Tasks.ReadWrite User.Read"

#: access_token 提前 60 秒视为过期，避免边界情况下的 401。
_EXPIRY_SKEW = 60

#: 设备码流程里「可重试」的错误码。
_PENDING_CODES = {"authorization_pending"}
_SLOW_DOWN_CODES = {"slow_down"}
_TERMINAL_CODES = {"authorization_declined", "expired_token", "bad_verification_code"}
#: 刷新失败后需要用户重新授权的错误码。
_REAUTH_CODES = {
    "invalid_grant",
    "interaction_required",
    "invalid_request",
    "unauthorized_client",
    "expired_token",
    "bad_verification_code",
}

LoginCallback = Callable[[str, "AccountStatus"], Awaitable[None]]


def _safe_json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


@dataclass
class DeviceCodeChallenge:
    """一次设备码授权中需要展示给用户的信息。"""

    user_code: str
    verification_uri: str
    message: str
    expires_at: float
    interval: int

    @property
    def expires_in_minutes(self) -> int:
        return max(1, int((self.expires_at - time.time()) // 60))

    def to_text(self) -> str:
        uri = self.verification_uri or "https://microsoft.com/devicelogin"
        return (
            "请在 **15 分钟内** 完成 Microsoft 授权：\n"
            f"1. 用手机或电脑浏览器打开：{uri}\n"
            f"2. 输入代码：`{self.user_code}`\n"
            f"3. 登录并同意「读取和写入任务」权限\n"
            f"完成后我会在这里通知你（剩余约 {self.expires_in_minutes} 分钟）。"
        )


@dataclass
class AccountStatus:
    """某个账号当前的授权状态。"""

    aid: str
    bound: bool = False
    upn: str | None = None
    display_name: str | None = None
    expires_at: float | None = None
    bound_at: float | None = None
    pending: bool = False
    session: str | None = None
    has_refresh_token: bool = False
    detail: str = ""

    @property
    def access_token_expires_in(self) -> int:
        if not self.expires_at:
            return 0
        return max(0, int(self.expires_at - time.time()))

    def to_text(self) -> str:
        if not self.bound:
            base = "尚未绑定 Microsoft 账号。发送 /todo login 开始绑定。"
            if self.pending:
                base += "（已有一个授权流程正在进行中）"
            return base
        who = self.display_name or "(未知账号)"
        lines = [
            "已绑定 Microsoft 账号：",
            f"- 账号：{who}" + (f" <{self.upn}>" if self.upn else ""),
            f"- access_token 剩余：{self.access_token_expires_in // 60} 分钟",
            f"- refresh_token：{'已保存（可自动续期）' if self.has_refresh_token else '缺失（下次需重新授权）'}",
        ]
        if self.pending:
            lines.append("- 注意：有一个新的授权流程正在进行，完成后会覆盖当前凭据")
        if self.detail:
            lines.append(f"- {self.detail}")
        return "\n".join(lines)


class DeviceCodeAuth:
    """管理设备码授权、token 续期与凭据存储。"""

    def __init__(
        self,
        *,
        star: Any,
        http: httpx.AsyncClient,
        config: Callable[[], dict],
        on_login: LoginCallback | None = None,
    ) -> None:
        """
        Args:
            star: 插件实例，用于读写插件级 KV。
            http: 共享的 httpx 异步客户端。
            config: 返回插件配置 dict 的可调用对象（每次读取都取最新配置）。
            on_login: 授权成功后的回调，用于主动通知用户。
        """
        self._store = AccountStore(star)
        self._http = http
        self._config = config
        self._on_login = on_login
        self._flows: dict[str, asyncio.Task] = {}
        self._challenges: dict[str, DeviceCodeChallenge] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ 配置

    def _tenant(self) -> str:
        tenant = str(self._config().get("tenant") or "common").strip() or "common"
        return tenant

    def _endpoint(self, leaf: str) -> str:
        return f"{AUTHORITY_TMPL.format(tenant=self._tenant())}/{leaf}"

    def _client_id(self) -> str:
        client_id = str(self._config().get("client_id") or "").strip()
        if not client_id:
            raise ConfigError(
                "尚未配置 client_id。请在 AstrBot WebUI 的插件配置里填写 Entra 应用的"
                "「应用程序(客户端) ID」。"
            )
        return client_id

    def _client_secret(self) -> str:
        return str(self._config().get("client_secret") or "").strip()

    def _scope(self) -> str:
        scope = str(self._config().get("scope") or "").strip()
        return scope or DEFAULT_SCOPE

    def _lock(self, aid: str) -> asyncio.Lock:
        lock = self._locks.get(aid)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[aid] = lock
        return lock

    # ------------------------------------------------------------------ HTTP

    async def _post_form(self, url: str, data: dict[str, str], *, retries: int = 2) -> dict:
        """POST 表单，失败时抛 ``AuthFlowError``（带 error code）。

        网络层错误（TLS 抖动、连接重置）会重试若干次：实测 ``login.microsoftonline.com``
        偶发 ``SSL: UNEXPECTED_EOF_WHILE_READING``，直接失败会让用户以为授权坏了。
        重试是安全的——设备码换 token 与 refresh 流程都不会因重复请求而失效。
        """
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = await self._http.post(url, data=data)
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                break
            payload = _safe_json(resp)
            error = payload.get("error")
            if error:
                desc = str(payload.get("error_description") or "").split("\r\n")[0]
                raise AuthFlowError(f"{desc or error}", code=str(error))
            if resp.status_code >= 400:
                raise AuthFlowError(
                    f"HTTP {resp.status_code}: {resp.text[:200]}",
                    code=f"http_{resp.status_code}",
                )
            return payload
        raise AuthFlowError(f"网络请求失败：{last_exc!s}", code="network_error") from last_exc

    # ----------------------------------------------------------- 授权流程

    async def start(self, aid: str, *, session: str) -> DeviceCodeChallenge:
        """发起设备码授权，返回需要展示给用户的信息，并后台轮询等待用户完成。"""
        client_id = self._client_id()
        await self._cancel_flow(aid)

        data = {"client_id": client_id, "scope": self._scope()}
        secret = self._client_secret()
        if secret:
            data["client_secret"] = secret

        payload = await self._post_form(self._endpoint("devicecode"), data)
        try:
            challenge = DeviceCodeChallenge(
                user_code=str(payload["user_code"]),
                verification_uri=str(
                    payload.get("verification_uri") or "https://microsoft.com/devicelogin"
                ),
                message=str(payload.get("message") or ""),
                expires_at=time.time() + float(payload.get("expires_in") or 900),
                interval=max(1, int(payload.get("interval") or 5)),
            )
            device_code = str(payload["device_code"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AuthFlowError(
                "Microsoft 未返回设备码，请稍后重试（或检查 tenant 配置）。",
                code="bad_response",
            ) from exc

        self._challenges[aid] = challenge
        task = asyncio.create_task(
            self._poll_until_done(aid, device_code, challenge, session),
            name=f"ms_todo_devicecode_{aid}",
        )
        self._flows[aid] = task
        task.add_done_callback(lambda t, a=aid: self._on_flow_done(a, t))
        logger.info(
            "device code flow started for %s (expires in %ss)",
            aid,
            int(challenge.expires_at - time.time()),
        )
        return challenge

    def _on_flow_done(self, aid: str, task: asyncio.Task) -> None:
        if self._flows.get(aid) is task:
            self._flows.pop(aid, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:  # 轮询任务内部已经兜底，这里只做记录
            logger.error("device code poll task failed for %s: %s", aid, exc)

    async def _poll_until_done(
        self,
        aid: str,
        device_code: str,
        challenge: DeviceCodeChallenge,
        session: str,
    ) -> None:
        interval = challenge.interval
        data = {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": self._client_id(),
            "device_code": device_code,
        }
        secret = self._client_secret()
        if secret:
            data["client_secret"] = secret

        while time.time() < challenge.expires_at:
            await asyncio.sleep(interval)
            try:
                payload = await self._post_form(self._endpoint("token"), data)
            except AuthFlowError as exc:
                code = exc.code or ""
                if code in _PENDING_CODES:
                    continue
                if code in _SLOW_DOWN_CODES:
                    interval += 5
                    continue
                if code in _TERMINAL_CODES:
                    logger.info("device code flow ended for %s: %s", aid, code)
                    return
                if code == "network_error":
                    logger.warning("device code poll network error for %s, retrying", aid)
                    continue
                logger.warning("device code poll failed for %s: %s", aid, exc)
                return

            try:
                await self._store_token(aid, payload, session=session)
            except GraphAPIError as exc:
                # 拿到 token 但读不到 /me 不算致命：凭据已经可用
                logger.warning("token stored but /me failed for %s: %s", aid, exc)
            logger.info("device code flow succeeded for %s", aid)
            return

        logger.info("device code flow expired for %s", aid)
        self._challenges.pop(aid, None)

    async def _store_token(self, aid: str, payload: dict, *, session: str) -> None:
        now = time.time()
        existing = await self._store.load(aid) or {}
        record: dict[str, Any] = {
            "access_token": str(payload.get("access_token") or ""),
            "refresh_token": str(payload.get("refresh_token") or "")
            or str(existing.get("refresh_token") or ""),
            "token_type": str(payload.get("token_type") or "Bearer"),
            "scope": str(payload.get("scope") or ""),
            "expires_at": now + float(payload.get("expires_in") or 3600),
            "obtained_at": now,
            "bound_at": float(existing.get("bound_at") or now),
            "upn": existing.get("upn"),
            "display_name": existing.get("display_name"),
            "session": session,
            "tenant": self._tenant(),
        }

        # 尽量补全账号标识，方便用户确认「绑的是哪个号」
        try:
            resp = await self._http.get(
                f"{GRAPH_BASE}/me?$select=userPrincipalName,displayName,mail",
                headers={"Authorization": f"Bearer {record['access_token']}"},
            )
            if resp.status_code < 400:
                me = _safe_json(resp)
                record["upn"] = me.get("userPrincipalName") or me.get("mail")
                record["display_name"] = me.get("displayName")
        except httpx.HTTPError as exc:
            logger.debug("fetch /me failed: %s", exc)

        await self._store.save(aid, record)
        self._challenges.pop(aid, None)

        status = AccountStatus(
            aid=aid,
            bound=True,
            upn=record.get("upn"),
            display_name=record.get("display_name"),
            expires_at=record["expires_at"],
            bound_at=record.get("bound_at"),
            session=session,
            has_refresh_token=bool(record.get("refresh_token")),
        )
        if self._on_login is not None:
            try:
                await self._on_login(aid, status)
            except Exception as exc:  # 通知失败不应影响授权结果
                logger.warning("login notify failed: %s", exc)

    # ----------------------------------------------------------- token 续期

    async def get_access_token(self, aid: str) -> str:
        """返回可用的 access_token，必要时自动续期。"""
        record = await self._store.load(aid)
        if not record or not record.get("access_token"):
            raise AuthRequiredError("尚未绑定 Microsoft 账号，无法执行该操作。")
        if float(record.get("expires_at") or 0) - _EXPIRY_SKEW > time.time():
            return str(record["access_token"])

        async with self._lock(aid):
            # 双重检查：可能已被其它协程刷新过
            record = await self._store.load(aid)
            if not record or not record.get("access_token"):
                raise AuthRequiredError("尚未绑定 Microsoft 账号，无法执行该操作。")
            if float(record.get("expires_at") or 0) - _EXPIRY_SKEW > time.time():
                return str(record["access_token"])

            refresh_token = str(record.get("refresh_token") or "")
            if not refresh_token:
                await self._store.delete(aid)
                raise AuthRequiredError("本地凭据缺少 refresh_token，需要重新授权。")

            data = {
                "grant_type": "refresh_token",
                "client_id": self._client_id(),
                "refresh_token": refresh_token,
                "scope": self._scope(),
            }
            secret = self._client_secret()
            if secret:
                data["client_secret"] = secret

            try:
                payload = await self._post_form(self._endpoint("token"), data)
            except AuthFlowError as exc:
                if (exc.code or "") in _REAUTH_CODES:
                    await self._store.delete(aid)
                    raise AuthRequiredError(
                        "Microsoft 授权已失效（refresh_token 过期或被撤销），需要重新授权。"
                    ) from exc
                raise

            record["access_token"] = str(payload.get("access_token") or "")
            record["refresh_token"] = str(payload.get("refresh_token") or refresh_token)
            record["expires_at"] = time.time() + float(payload.get("expires_in") or 3600)
            record["obtained_at"] = time.time()
            record["scope"] = str(payload.get("scope") or record.get("scope") or "")
            await self._store.save(aid, record)
            logger.info("access token refreshed for %s", aid)
            return str(record["access_token"])

    # --------------------------------------------------------------- 其它

    async def status(self, aid: str) -> AccountStatus:
        record = await self._store.load(aid)
        pending = aid in self._flows
        if not record:
            return AccountStatus(aid=aid, bound=False, pending=pending)
        return AccountStatus(
            aid=aid,
            bound=bool(record.get("access_token") or record.get("refresh_token")),
            upn=record.get("upn"),
            display_name=record.get("display_name"),
            expires_at=float(record["expires_at"]) if record.get("expires_at") else None,
            bound_at=float(record["bound_at"]) if record.get("bound_at") else None,
            pending=pending,
            session=record.get("session"),
            has_refresh_token=bool(record.get("refresh_token")),
        )

    async def mark_access_token_expired(self, aid: str) -> None:
        """强制下一次 ``get_access_token`` 走续期。

        用于 Graph 在 token 到期前就返回 401 的场景（密钥轮换、管理员撤销等）。
        """
        record = await self._store.load(aid)
        if not record:
            return
        record["expires_at"] = 0
        await self._store.save(aid, record)

    async def session_of(self, aid: str) -> str | None:
        """返回该账号最近一次交互的会话（用于主动推送）。"""
        record = await self._store.load(aid)
        if not record:
            return None
        session = record.get("session")
        return str(session) if session else None

    async def pending_challenge(self, aid: str) -> DeviceCodeChallenge | None:
        return self._challenges.get(aid)

    async def logout(self, aid: str) -> None:
        await self._cancel_flow(aid)
        await self._store.delete(aid)
        logger.info("account unbound: %s", aid)

    async def _cancel_flow(self, aid: str) -> None:
        self._challenges.pop(aid, None)
        task = self._flows.pop(aid, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - 清理路径不关心具体原因
                logger.debug("cancel device code flow for %s raised: %s", aid, exc)

    async def close(self) -> None:
        """插件卸载/重载时取消所有未完成的授权轮询。"""
        for aid in list(self._flows):
            await self._cancel_flow(aid)
