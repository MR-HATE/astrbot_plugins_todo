"""``graph/planner.py`` 的用例：时间解析、归一化、去重、渲染。

这些是纯函数，覆盖边界最多、回归价值最高。
"""

from __future__ import annotations

from datetime import date, datetime

from conftest import at, days, today
from astrbot_plugins_todo.graph import planner as P
from astrbot_plugins_todo.graph.models import TaskDraft

BASE = date(2026, 9, 14)  # 周一


# ---------------------------------------------------------------- 日期


def test_resolve_date_absolute_and_relative():
    assert P.resolve_date("2026-09-18", BASE)[0] == "2026-09-18"
    assert P.resolve_date("2026-9-5", BASE)[0] == "2026-09-05"
    assert P.resolve_date("今天", BASE)[0] == "2026-09-14"
    assert P.resolve_date("明天", BASE)[0] == "2026-09-15"
    assert P.resolve_date("后天", BASE)[0] == "2026-09-16"
    assert P.resolve_date("大后天", BASE)[0] == "2026-09-17"
    assert P.resolve_date("3天后", BASE)[0] == "2026-09-17"


def test_resolve_date_weekdays():
    # 周一为基准：最近的周三是 9/16，「下周三」是下一个自然周的周三
    assert P.resolve_date("周三", BASE)[0] == "2026-09-16"
    assert P.resolve_date("星期一", BASE)[0] == "2026-09-14"
    assert P.resolve_date("下周三", BASE)[0] == "2026-09-23"
    assert P.resolve_date("下周一", BASE)[0] == "2026-09-21"


def test_resolve_date_month_day_rolls_to_next_year():
    # 已经过去的日子顺延到明年
    assert P.resolve_date("9月18日", BASE)[0] == "2026-09-18"
    assert P.resolve_date("1月5日", BASE)[0] == "2027-01-05"
    assert P.resolve_date("12/01", BASE)[0] == "2026-12-01"


def test_resolve_date_rejects_garbage_with_warning():
    value, warning = P.resolve_date("随便写", BASE)
    assert value is None and warning


def test_resolve_date_rejects_invalid_calendar_date():
    value, warning = P.resolve_date("2026-02-30", BASE)
    assert value is None and warning


# ---------------------------------------------------------------- 时刻


def test_resolve_time_arabic():
    assert P.resolve_time("9:00")[0] == "09:00"
    assert P.resolve_time("9点")[0] == "09:00"
    assert P.resolve_time("9点30")[0] == "09:30"
    assert P.resolve_time("9点半")[0] == "09:30"
    assert P.resolve_time("9点一刻")[0] == "09:15"
    assert P.resolve_time("9点三刻")[0] == "09:45"


def test_resolve_time_meridiem_and_chinese_numerals():
    assert P.resolve_time("下午3点")[0] == "15:00"
    assert P.resolve_time("晚上8点")[0] == "20:00"
    assert P.resolve_time("九点半")[0] == "09:30"
    assert P.resolve_time("下午三点")[0] == "15:00"
    assert P.resolve_time("晚上十点")[0] == "22:00"
    assert P.resolve_time("十二点")[0] == "12:00"
    assert P.resolve_time("上午十二点")[0] == "00:00"
    assert P.resolve_time("中午十二点")[0] == "12:00"
    assert P.resolve_time("九:三十")[0] == "09:30"


def test_resolve_time_rejects_out_of_range_and_garbage():
    assert P.resolve_time("25:00")[0] is None
    assert P.resolve_time("乱写")[0] is None


def test_split_date_time():
    assert P.split_date_time("9月18日 20:00") == ("9月18日", "20:00")
    assert P.split_date_time("9月18日20:00") == ("9月18日", "20:00")
    assert P.split_date_time("明天 晚上8点") == ("明天", "晚上8点")
    assert P.split_date_time("下周三下午三点") == ("下周三", "下午三点")
    # 纯时刻不拆分，纯日期也不拆分
    assert P.split_date_time("晚上8点") == ("晚上8点", None)
    assert P.split_date_time("明天") == ("明天", None)


# ---------------------------------------------------------------- 提醒


def test_parse_remind_naive_and_aware_inputs():
    # 不带偏移量：按本地时间理解
    assert P.parse_remind("2026-09-16T20:00", "Asia/Shanghai")[0] == "2026-09-16T20:00"
    # 带 Z：换算到配置时区（UTC 12:00 -> 上海 20:00）
    value, note = P.parse_remind("2026-09-16T12:00:00Z", "Asia/Shanghai")
    assert value == "2026-09-16T20:00" and note
    # 带 +08:00：不触发换算提示
    assert P.parse_remind("2026-09-16T20:00:00+08:00", "Asia/Shanghai")[0] == "2026-09-16T20:00"


def test_parse_remind_accepts_graph_style_dict():
    value, _ = P.parse_remind(
        {"dateTime": "2026-09-16T20:00:00", "timeZone": "China Standard Time"},
        "Asia/Shanghai",
    )
    assert value == "2026-09-16T20:00"


def test_parse_remind_time_only_falls_back_to_due_date():
    # 只给时刻时落在截止日当天
    assert P.parse_remind("下午三点", "Asia/Shanghai", fallback_date=days(3))[0] == f"{days(3)}T15:00"


def test_parse_remind_rejects_past_time():
    value, warning = P.parse_remind("2020-01-01T09:00:00+08:00", "Asia/Shanghai")
    assert value is None and warning


def test_parse_remind_chinese_date_with_clock():
    value, _ = P.parse_remind(f"{days(5)} 20:00", "Asia/Shanghai")
    assert value == f"{days(5)}T20:00"


# ---------------------------------------------------------------- 重复与 cron


def test_normalize_repeat_aliases():
    assert P.normalize_repeat("每天")[0] == "daily"
    assert P.normalize_repeat("每周")[0] == "weekly"
    assert P.normalize_repeat("每月")[0] == "monthly"
    assert P.normalize_repeat("daily")[0] == "daily"
    assert P.normalize_repeat("none")[0] is None
    assert P.normalize_repeat(None)[0] is None
    assert P.normalize_repeat("随便")[0] is None


def test_cron_expression_for():
    # 2026-09-16 是周三
    assert P.cron_expression_for("2026-09-16T20:00", "daily") == "0 20 * * *"
    assert P.cron_expression_for("2026-09-16T20:00", "weekly") == "0 20 * * wed"
    assert P.cron_expression_for("2026-09-16T20:00", "monthly") == "0 20 16 * *"
    assert P.cron_expression_for("2026-09-16T20:00", None) is None


# ---------------------------------------------------------------- 任务归一化


def test_normalize_tasks_importance_and_steps():
    tasks = P.normalize_tasks(
        [
            {"title": "  订  酒店 ", "importance": "很重要", "steps": ["比价", "下单", "比价", "  "]},
            {"title": "随便的事", "importance": "说不清"},
        ],
        "Asia/Shanghai",
    )
    assert tasks[0].title == "订 酒店"  # 空白被压缩
    assert tasks[0].importance == "high"
    assert tasks[0].steps == ["比价", "下单"]  # 去重 + 去空
    assert tasks[1].importance == "normal" and tasks[1].warnings


def test_normalize_tasks_drops_empty_title_and_keeps_plain_string():
    tasks = P.normalize_tasks([{"title": "   "}, "直接给字符串"], "Asia/Shanghai")
    assert [t.title for t in tasks] == ["直接给字符串"]


def test_normalize_tasks_remote_past_due_and_dropped_remind():
    tasks = P.normalize_tasks(
        [
            {"title": "过期的", "due_date": days(-2)},
            {"title": "提醒已过", "remind_at": "2020-01-01T09:00"},
        ],
        "Asia/Shanghai",
    )
    assert any("早于今天" in w for w in tasks[0].warnings)
    assert tasks[1].remind_at is None and any("过去" in w for w in tasks[1].warnings)


def test_normalize_tasks_repeat_without_remind_is_ignored():
    task = P.normalize_tasks([{"title": "x", "remind_repeat": "每天"}], "Asia/Shanghai")[0]
    assert task.remind_repeat is None and any("重复" in w for w in task.warnings)


def test_normalize_tasks_clears_time_without_date():
    task = P.normalize_tasks([{"title": "x", "due_time": "18:00"}], "Asia/Shanghai")[0]
    assert task.due_time is None and any("只有时刻" in w for w in task.warnings)


# ---------------------------------------------------------------- 去重


def test_fingerprint_is_list_sensitive_and_ignores_spaces():
    a = TaskDraft(title="交 房租", due_date="2026-10-01")
    b = TaskDraft(title="交房租", due_date="2026-10-01")
    assert a.fingerprint("生活") == b.fingerprint("生活")
    assert a.fingerprint("生活") != a.fingerprint("工作")


def test_dedupe_batch_keeps_first():
    tasks = [
        TaskDraft(title="x", due_date=days(1)),
        TaskDraft(title="x", due_date=days(1)),
        TaskDraft(title="y"),
    ]
    unique, duplicated = P.dedupe_batch(tasks, "L")
    assert [t.title for t in unique] == ["x", "y"] and [t.title for t in duplicated] == ["x"]


def test_apply_seen_skips_known_keys():
    a = TaskDraft(title="a", due_date=days(1))
    b = TaskDraft(title="b", due_date=days(1))
    seen = {a.fingerprint("L"): {"task_id": "t1"}}
    fresh, skipped = P.apply_seen([a, b], "L", seen)
    assert [t.title for t in fresh] == ["b"] and [t.title for t in skipped] == ["a"]


# ---------------------------------------------------------------- 渲染


def test_format_due_relative_and_completed_switch():
    assert P.format_due(days(0), None, today()).startswith("今天")
    assert P.format_due(days(-2), None, today()).startswith("已逾期 2 天")
    # relative=False 时不带相对说明（已完成的任务用它，避免"已完成·已逾期"）
    weekday = P.weekday_cn(date.fromisoformat(days(-2)))
    assert P.format_due(days(-2), None, today(), relative=False) == f"{days(-2)}（{weekday}）"
    assert P.format_due(None, None, today()) == "未设截止"


def test_render_preview_shows_repeat_and_warnings():
    tasks = [
        TaskDraft(title="记账", remind_at=at(1, "21:00"), remind_repeat="daily"),
        TaskDraft(title="x", warnings=["日期没识别"]),
    ]
    text = P.render_preview("生活", tasks, "abcd1234", "Asia/Shanghai")
    assert "每天重复" in text and "abcd1234" in text and "日期没识别" in text


def test_render_import_result_variants():
    created = [(TaskDraft(title="a", due_date=days(1)), "t1")]
    text = P.render_import_result("生活", created, [], [], tz_name="Asia/Shanghai")
    assert "成功 1 项" in text and "a" in text

    skipped = [TaskDraft(title="b")]
    text = P.render_import_result("生活", [], skipped, [], tz_name="Asia/Shanghai")
    assert "没有新增待办" in text and "跳过 1 项" in text

    failed = [(TaskDraft(title="c"), "boom")]
    text = P.render_import_result("生活", [], [], failed, tz_name="Asia/Shanghai")
    assert "失败 1 项" in text and "boom" in text


def test_plan_expired():
    now = 10_000.0
    assert P.plan_expired(now - 10, now=now) is False
    assert P.plan_expired(now - P.PENDING_TTL - 1, now=now) is True


# ---------------------------------------------------------------- 查询过滤


def _graph_task(title, status="notStarted", due=None, importance="normal"):
    task = {"id": title, "title": title, "status": status, "importance": importance}
    if due:
        task["dueDateTime"] = {"dateTime": f"{due}T09:00:00.0000000"}
    return task


def test_task_due_parsing():
    raw = {"dueDateTime": {"dateTime": "2026-09-16T18:00:00.0000000"}}
    assert P.task_due(raw) == ("2026-09-16", "18:00")
    assert P.task_due({}) == (None, None)


def test_filter_tasks_scopes():
    data = [
        _graph_task("逾期", due=days(-2)),
        _graph_task("今天", due=days(0)),
        _graph_task("未来", due=days(3)),
        _graph_task("十天后", due=days(10)),
        _graph_task("已完成", status="completed", due=days(-1)),
        _graph_task("没日期"),
    ]
    def names(scope: str) -> list[str]:
        return [t["title"] for t in P.filter_tasks(data, scope, "Asia/Shanghai")]
    assert set(names("pending")) == {"逾期", "今天"}
    assert names("overdue") == ["逾期"]
    assert names("today") == ["今天"]
    assert names("upcoming") == ["未来"]
    assert names("completed") == ["已完成"]
    assert len(names("all")) == 6
    # 未知 scope 按 all 处理
    assert len(names("随便")) == 6


def test_sort_tasks_overdue_first_and_undated_last():
    data = [_graph_task("没日期"), _graph_task("今天", due=days(0)), _graph_task("逾期", due=days(-1))]
    assert [t["title"] for t in P.sort_tasks(data)] == ["逾期", "今天", "没日期"]


def test_render_task_list_shows_status_and_list_name():
    data = [_graph_task("买菜", due=days(0))]
    data[0]["_list_name"] = "生活"
    text = P.render_task_list(data, scope="pending", list_label="全部列表", tz_name="Asia/Shanghai")
    assert "⬜ 买菜" in text and "列表「生活」" in text


def test_local_now_returns_aware_datetime():
    value = P.local_now("Asia/Shanghai")
    assert isinstance(value, datetime) and value.tzinfo is not None


def test_get_tz_falls_back_on_invalid_name():
    assert P.get_tz("Not/AZone") is not None
