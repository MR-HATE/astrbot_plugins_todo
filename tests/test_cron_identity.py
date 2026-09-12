"""回归：定时任务唤起时，账号 key 必须仍然算得对。

背景（真实事故）：AstrBot 的定时任务用**合成事件**唤醒 Agent，其 PlatformMetadata.name
被写死为 ``cron``（见 ``core/cron/events.py``），而插件用「平台:用户」当账号 key。
于是到点后 Agent 调用待办工具时算出 ``cron:1747831170``，与绑定时的
``aiocqhttp:1747831170`` 对不上，误报「尚未绑定 Microsoft 账号」——
凭据其实好好的。

修法：平台名不可信时，从 ``unified_msg_origin``（合成事件里保留的是原始会话）取回；
新创建的定时任务还会把原始 aid 直接写进 payload。
"""

from __future__ import annotations

from conftest import at, cron_event, make_harness

SESSION = "aiocqhttp:private:1747831170"
QQ_ID = "1747831170"


def test_account_id_survives_cron_event_without_payload():
    """兼容修复前创建的任务：payload 里没有 aid，也要从会话串还原平台名。"""
    h = make_harness()
    event = cron_event(SESSION, QQ_ID)
    assert h.plugin.account_id(event) == f"aiocqhttp:{QQ_ID}"


def test_account_id_prefers_explicit_aid_in_payload():
    h = make_harness()
    event = cron_event(SESSION, QQ_ID, payload={"aid": "aiocqhttp:1747831170"})
    assert h.plugin.account_id(event) == "aiocqhttp:1747831170"


def test_account_id_unchanged_for_normal_events():
    h = make_harness()
    assert h.plugin.account_id(h.event(sender=QQ_ID)) == f"aiocqhttp:{QQ_ID}"


def test_cron_platform_without_session_falls_back_to_platform_name():
    """会话也拿不到时的兜底：不至于抛异常，只是仍然算不出原账号。"""
    h = make_harness()
    event = cron_event("cron:other:whatever", "astrbot")
    assert h.plugin.account_id(event) == "cron:astrbot"


def test_reminder_payload_carries_aid():
    h = make_harness()
    h.import_now(
        h.event(sender=QQ_ID),
        [{"title": "喝水", "remind_at": at(1, "20:00")}],
        plan_name="生活",
    )
    assert h.cron.jobs[0]["payload"]["aid"] == f"aiocqhttp:{QQ_ID}"


def test_cron_woken_agent_can_read_tasks():
    """到点后 Agent 调工具读待办——修好之前这里会回「尚未绑定」。"""
    h = make_harness()
    h.import_now(h.event(sender=QQ_ID), [{"title": "喝水"}], plan_name="生活")

    payload = h.cron.jobs[0]["payload"] if h.cron.jobs else None
    woke = cron_event(SESSION, QQ_ID, payload=payload)
    out = h.list_tasks(woke, {"scope": "all"})
    assert "尚未绑定" not in out
    assert "喝水" in out


def test_cron_woken_agent_can_import_tasks():
    """到点后 Agent 也可以写待办（如每天自动生成的例行清单）。"""
    h = make_harness()
    h.import_now(h.event(sender=QQ_ID), [{"title": "占位"}], plan_name="生活")

    # 修复前创建的老任务：payload 里没有 aid
    legacy_payload = {"session": SESSION, "sender_id": QQ_ID, "origin": "plugin"}
    woke = cron_event(SESSION, QQ_ID, payload=legacy_payload)
    out = h.import_now(woke, [{"title": "例行检查"}], plan_name="生活")
    assert "成功 1 项" in out
    assert "尚未绑定" not in out
