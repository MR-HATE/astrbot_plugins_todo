"""回归：定时任务唤起时，账号 key 必须仍然算得对。

真实事故（踩了两次）：

1. AstrBot 的定时任务用**合成事件**（``CronMessageEvent``）唤醒 Agent，
   其 ``PlatformMetadata.name`` 被写死为 ``cron``；
2. ``unified_msg_origin`` 的第一段是平台**实例 id**（配置里的 ``id``，如 ``yume``），
   **不是**平台类型（``aiocqhttp``）。

插件用「平台类型:用户ID」当账号 key。这两点各错一次，key 就会算成 ``cron:xxx``
或 ``yume:xxx``，与绑定时的 ``aiocqhttp:xxx`` 对不上，工具便误报
「尚未绑定 Microsoft 账号」——凭据其实完好。

**教训**：fixture 里的会话串必须让"实例 id ≠ 平台类型"（见 conftest 的 PLATFORM_ID /
PLATFORM_TYPE）。最初这组用例把两者写成同一个值，于是全绿却测不到真坑。
"""

from __future__ import annotations

from conftest import (
    PLATFORM_ID,
    PLATFORM_TYPE,
    QQ_ID,
    SESSION,
    FakeMeta,
    FakePlatform,
    at,
    cron_event,
    make_harness,
)

EXPECTED_AID = f"{PLATFORM_TYPE}:{QQ_ID}"


def test_account_id_resolves_instance_id_to_platform_type():
    """核心回归：umo 第一段是实例 id（yume），必须还原成类型（aiocqhttp）。"""
    h = make_harness()
    event = cron_event(SESSION, QQ_ID)
    assert SESSION.startswith(f"{PLATFORM_ID}:")  # 前提：fixture 里两者确实不同
    assert PLATFORM_ID != PLATFORM_TYPE
    assert h.plugin.account_id(event) == EXPECTED_AID


def test_account_id_prefers_explicit_aid_in_payload():
    h = make_harness()
    event = cron_event(SESSION, QQ_ID, payload={"aid": EXPECTED_AID})
    assert h.plugin.account_id(event) == EXPECTED_AID


def test_account_id_unchanged_for_normal_events():
    """普通事件直接取 get_platform_name()，行为不变。"""
    h = make_harness()
    assert h.plugin.account_id(h.event(sender=QQ_ID)) == EXPECTED_AID


def test_account_id_falls_back_to_instance_id_when_unknown():
    """适配器已卸载、查不到类型时退回实例 id——算不出原账号，但不能抛异常。"""
    h = make_harness(platforms={})  # 注册表里没有这个实例
    event = cron_event(SESSION, QQ_ID)
    assert h.plugin.account_id(event) == f"{PLATFORM_ID}:{QQ_ID}"


def test_account_id_falls_back_when_session_has_no_platform():
    h = make_harness()
    event = cron_event("cron:OtherMessage:whatever", "astrbot")
    assert h.plugin.account_id(event) == "cron:astrbot"


def test_reminder_payload_carries_aid():
    h = make_harness()
    h.import_now(
        h.event(sender=QQ_ID),
        [{"title": "喝水", "remind_at": at(1, "20:00")}],
        plan_name="生活",
    )
    assert h.cron.jobs[0]["payload"]["aid"] == EXPECTED_AID


def test_cron_woken_agent_can_read_tasks():
    """到点后 Agent 调工具读待办——修好之前这里会回「尚未绑定」。"""
    h = make_harness()
    h.import_now(h.event(sender=QQ_ID), [{"title": "喝水"}], plan_name="生活")

    # 刻意用没有 aid 的老 payload，走"从会话串还原平台类型"这条路径
    legacy_payload = {"session": SESSION, "sender_id": QQ_ID, "origin": "plugin"}
    woke = cron_event(SESSION, QQ_ID, payload=legacy_payload)
    out = h.list_tasks(woke, {"scope": "all"})
    assert "尚未绑定" not in out
    assert "喝水" in out


def test_cron_woken_agent_with_legacy_payload_can_import():
    """修复前创建的老任务：payload 里没有 aid，也要能还原账号。"""
    h = make_harness()
    h.import_now(h.event(sender=QQ_ID), [{"title": "占位"}], plan_name="生活")

    legacy_payload = {"session": SESSION, "sender_id": QQ_ID, "origin": "plugin"}
    woke = cron_event(SESSION, QQ_ID, payload=legacy_payload)
    out = h.import_now(woke, [{"title": "例行检查"}], plan_name="生活")
    assert "成功 1 项" in out
    assert "尚未绑定" not in out


def test_reminder_created_on_platform_whose_id_differs_from_type():
    """提醒的"平台是否支持主动推送"也按实例 id 查适配器，不能再比类型白名单。"""
    h = make_harness()
    out = h.import_now(
        h.event(sender=QQ_ID),
        [{"title": "喝水", "remind_at": at(1, "20:00")}],
        plan_name="生活",
    )
    assert len(h.cron.jobs) == 1
    assert "不支持机器人主动发消息" not in out


def test_platform_without_proactive_support_skips_reminder():
    """适配器声明 support_proactive_message=False 时如实告知，不静默创建。"""
    platforms = {
        PLATFORM_ID: FakePlatform(),
        "nomail": FakePlatform(
            FakeMeta(name="nomail", platform_id="nomail", support_proactive_message=False)
        ),
    }
    h = make_harness(platforms=platforms)
    out = h.import_now(
        h.event(platform="nomail", sender="1", platform_id="nomail"),
        [{"title": "喝水", "remind_at": at(1, "20:00")}],
        plan_name="生活",
    )
    assert h.cron.jobs == []
    assert "不支持机器人主动发消息" in out
