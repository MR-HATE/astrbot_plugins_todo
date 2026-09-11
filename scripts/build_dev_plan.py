"""生成《开发计划》Excel。

用法：
    python scripts/build_dev_plan.py [输出路径]

默认输出到插件根目录的 ``开发计划.xlsx``。计划随里程碑推进变化时，
直接改本文件的表格数据再重新运行即可（不依赖任何第三方模板）。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# ---------------------------------------------------------------- 样式

HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=11)
TITLE_FONT = Font(bold=True, size=14, color="1F4E79")
DONE_FILL = PatternFill("solid", fgColor="E2EFDA")
DOING_FILL = PatternFill("solid", fgColor="FFF2CC")
MILESTONE_FILL = PatternFill("solid", fgColor="DDEBF7")
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(vertical="top", wrap_text=True)
CENTER = Alignment(vertical="center", horizontal="center", wrap_text=True)

STATUS_FILL = {
    "已完成": DONE_FILL,
    "待验收": DOING_FILL,
    "进行中": DOING_FILL,
}

# ---------------------------------------------------------------- 数据

PLAN_HEADERS = ["里程碑", "序号", "任务", "具体做法 / 验收标准", "交付物", "依赖", "状态", "预估(人时)"]

PLAN_ROWS: list[list[str]] = [
    # ---------------- M1
    ["M1 骨架与授权闭环", "1.1", "修正 metadata / 配置骨架，替换 helloworld 模板",
     "metadata.yaml 的 name/version/astrbot_version 规范化；_conf_schema.json 重写为真实配置项；新增 requirements.txt", "metadata.yaml, _conf_schema.json, requirements.txt", "-", "已完成", "1"],
    ["M1 骨架与授权闭环", "1.2", "凭据存储（按用户隔离）",
     "graph/store.py：以「平台:发送者ID」为 key 存进 AstrBot 插件级 KV（shared_preferences），并维护账号索引", "graph/store.py", "1.1", "已完成", "2"],
    ["M1 骨架与授权闭环", "1.3", "OAuth 2.0 设备码授权 + 自动续期",
     "graph/auth.py：/devicecode 取码 → 后台按 interval 轮询 /token → 存 refresh_token；access_token 过期前 60s 自动续期；per-user asyncio.Lock 防并发刷新；invalid_grant 时删除凭据并提示重新授权", "graph/auth.py", "1.2", "已完成", "6"],
    ["M1 骨架与授权闭环", "1.4", "Graph REST 封装",
     "graph/client.py：自动带 token、401 强制续期重试一次、429/5xx 按 Retry-After 或指数退避、错误统一转 GraphAPIError（含 request-id）", "graph/client.py, graph/errors.py", "1.3", "已完成", "4"],
    ["M1 骨架与授权闭环", "1.5", "聊天指令",
     "/todo login|status|logout|lists 四个子指令；指令组自动渲染帮助树", "main.py", "1.3, 1.4", "已完成", "3"],
    ["M1 骨架与授权闭环", "1.6", "认证类 LLM 工具",
     "ms_todo_account_status / ms_todo_login_start / ms_todo_logout / ms_todo_list_task_lists；注册后对齐 handler_module_path，规避 AstrBot#8578 导致 reload 后工具被静默禁用", "tools/todo_tools.py", "1.5", "已完成", "3"],
    ["M1 骨架与授权闭环", "1.7", "README 与 Entra 注册指引",
     "写清「只支持委派权限」「启用公共客户端流」「重定向 URI 留空」「21Vianet 不支持」等关键前提", "README.md", "-", "已完成", "2"],
    ["M1 骨架与授权闭环", "1.8", "真机验收",
     "在本机 AstrBot 完成一次 /todo login → 手机端授权 → /todo lists 返回真实列表；重启 AstrBot 后凭据仍可用（验证 KV 持久化）", "验收记录（截图/日志）", "1.1-1.7", "已完成", "1"],

    # ---------------- M2
    ["M2 导入链路（工具与预览确认）", "2.1", "数据模型与 JSON Schema",
     "TaskDraft（title/due_date/due_time/importance/note/remind_at/steps/source_key）与 PlanDraft；在工具 parameters 中固化 Schema，约束模型输出", "graph/models.py", "M1", "已完成", "3"],
    ["M2 导入链路（工具与预览确认）", "2.2", "归一化、校验、去重、预览渲染",
     "相对时间→绝对日期（含 今天/明天/下周三/N天后/9月18日 兜底解析，基准配置 timezone）；标题/优先级/提醒校验；source_key 去重；中文预览卡片与结果回执", "graph/planner.py", "2.1", "已完成", "5"],
    ["M2 导入链路（工具与预览确认）", "2.3", "ms_todo_import_tasks（两阶段提交）",
     "confirmed=false：暂存 plan 到 KV 并返回预览；confirmed=true：批量创建（并发 3）并回写结果；require_confirm=false 时允许直接写入", "tools/todo_tools.py, main.py", "2.2", "已完成", "6"],
    ["M2 导入链路（工具与预览确认）", "2.4", "按计划名自动创建/复用列表",
     "auto_create_list=true 时用计划名（或用户自定义标题）匹配已有列表，不存在则 POST /me/todo/lists 新建；关闭时回退默认列表；默认列表始终保证存在", "graph/client.py, main.py", "2.2", "已完成", "3"],
    ["M2 导入链路（工具与预览确认）", "2.5", "指令兜底通道",
     "/todo import <自然语言> 走 context.llm_generate 抽取结构化任务（复用同一 Schema 与校验）；/todo confirm、/todo cancel 完成二次确认", "main.py", "2.3", "已完成", "5"],
    ["M2 导入链路（工具与预览确认）", "2.6", "端到端验收",
     "一句话 → 预览 → 确认 → Microsoft To Do App 出现任务；日期/重要性/子步骤正确；重复导入不产生重复任务（已用假 Graph 客户端跑通全链路，待真机验收）", "验收记录", "2.1-2.5", "待验收", "2"],

    # ---------------- M3
    ["M3 Skill（Agent 操作手册）", "3.1", "编写 SKILL.md",
     "skills/ms-todo-import/SKILL.md：触发条件、意图边界、任务拆分粒度、时间归一化规则、列表选择优先级、强制预览确认流程、输出模板、异常降级话术、反例清单；纯说明书不含脚本（避免 Local/Sandbox 代码执行限制）", "skills/ms-todo-import/SKILL.md", "M2", "待开始", "5"],
    ["M3 Skill（Agent 操作手册）", "3.2", "渐进式披露的参考资源",
     "references/time-parsing.md（今天/明天/这周五/下周三/月底/每月10号等规则表）、references/examples.md（正例与反例）", "skills/ms-todo-import/references/*", "3.1", "待开始", "3"],
    ["M3 Skill（Agent 操作手册）", "3.3", "边界与误触发验证",
     "闲聊、纯提问、与待办无关的指令不应触发；一次导入只问一个澄清问题；确认前绝不写入", "测试记录", "3.1, 3.2", "待开始", "2"],
    ["M3 Skill（Agent 操作手册）", "3.4", "验收",
     "「下周要去上海出差，周三前订酒店」→ 自动走完预览确认并写入；Skill 在 WebUI Skills 页以插件来源只读展示", "验收记录", "3.1-3.3", "待开始", "1"],

    # ---------------- M4
    ["M4 查询、管理与 WebUI", "4.1", "查询类工具",
     "ms_todo_list_tasks（scope=today/overdue/upcoming/all）；/todo today 指令", "tools/todo_tools.py", "M2", "待开始", "3"],
    ["M4 查询、管理与 WebUI", "4.2", "增删改类工具",
     "ms_todo_update_task / ms_todo_complete_task / ms_todo_delete_task / ms_todo_add_checklist_items；删除类操作二次确认", "tools/todo_tools.py", "4.1", "待开始", "5"],
    ["M4 查询、管理与 WebUI", "4.3", "批量与限流优化",
     "POST /$batch 每批 ≤20 个请求；失败分批回退为并发 3 + 429 退避；记录部分失败明细", "graph/client.py", "4.2", "待开始", "4"],
    ["M4 查询、管理与 WebUI", "4.4", "幂等与去重",
     "source_key 写入本地 KV；可选 openTypeExtension（extensionName 仅字母数字下划线）写进任务本身，换机器也不重复", "graph/planner.py", "4.3", "待开始", "4"],
    ["M4 查询、管理与 WebUI", "4.5", "WebUI 绑定/管理页",
     "插件 Pages：列出已绑定用户与状态、解绑、修改默认列表、生成授权链接/设备码；页面通过 register_web_api 注册的后端接口读写", "pages/index.html, main.py", "M1", "待开始", "8"],
    ["M4 查询、管理与 WebUI", "4.6", "WebUI 接口权限校验",
     "所有 Web API 校验 AstrBot 管理员身份；返回数据不包含 token 明文", "main.py", "4.5", "待开始", "2"],

    # ---------------- M5
    ["M5 定时提醒（复用 AstrBot 内置定时任务）", "5.1", "提醒时间解析",
     "支持「出发前一天 20:00」「每天早上 8 点」等；无提醒时间时不创建定时任务，仅用 To Do 自身提醒", "graph/planner.py", "M2", "待开始", "3"],
    ["M5 定时提醒（复用 AstrBot 内置定时任务）", "5.2", "调用内置 cron 创建提醒",
     "context.cron_manager.add_active_job(name=..., run_once=True, run_at=..., timezone=..., payload={session, sender_id, note, origin})；周期提醒用 cron_expression", "graph/reminder.py", "5.1", "待开始", "5"],
    ["M5 定时提醒（复用 AstrBot 内置定时任务）", "5.3", "任务与 job 关联",
     "导入时记录 task_id ↔ job_id 映射；任务完成/删除时同步 delete_job，避免僵尸提醒", "graph/reminder.py", "5.2", "待开始", "3"],
    ["M5 定时提醒（复用 AstrBot 内置定时任务）", "5.4", "平台能力与降级",
     "仅在支持主动消息的平台（aiocqhttp/telegram/discord/slack/lark 等）创建提醒；不支持时明确告知用户改用 To Do 自提醒", "main.py", "5.2", "待开始", "2"],
    ["M5 定时提醒（复用 AstrBot 内置定时任务）", "5.5", "验收",
     "到点机器人在原会话主动提醒；WebUI「未来任务」页可见该任务；删除待办后提醒同步消失", "验收记录", "5.1-5.4", "待开始", "2"],

    # ---------------- M6
    ["M6 测试与发布", "6.1", "单元测试",
     "Graph 调用打桩、时间归一化、去重、预览渲染的 pytest 用例；覆盖 401/429/403/网络异常分支", "tests/", "M4, M5", "待开始", "8"],
    ["M6 测试与发布", "6.2", "手动端到端清单",
     "含失败路径：未授权、refresh 失效、限流、21Vianet/企业租户 403、代理不可用", "tests/manual-checklist.md", "6.1", "待开始", "3"],
    ["M6 测试与发布", "6.3", "代码规范",
     "ruff format + ruff check 通过；类型注解完整；无 requests 依赖", "-", "M4, M5", "待开始", "2"],
    ["M6 测试与发布", "6.4", "文档补全",
     "README 配置说明、隐私与安全说明、常见问题（为何必须授权一次、如何撤销授权）", "README.md", "M5", "待开始", "3"],
    ["M6 测试与发布", "6.5", "发布",
     "按 AstrBot 插件市场规范检查（目录名/metadata/logo/版本号），打 tag 并更新 README", "GitHub Release", "6.1-6.4", "待开始", "2"],
]

DECISION_HEADERS = ["决策点", "你的选择", "设计影响"]
DECISION_ROWS = [
    ["账号与范围", "每个用户各自绑定；支持从 WebUI 和聊天界面绑定",
     "凭据以「平台:发送者ID」为 key 隔离存储，群聊内互不覆盖；聊天侧已提供 /todo login 与 ms_todo_login_start；WebUI 管理页排在 M4（插件 Pages + register_web_api，仅管理员可访问，不返回 token 明文）"],
    ["写入策略", "做成开关：默认「预览 → 确认 → 写入」",
     "配置项 require_confirm（默认 true）。工具采用两阶段提交：confirmed=false 只暂存 KV 并返回预览，confirmed=true 才写 Graph；另提供 /todo confirm|cancel 作为指令兜底"],
    ["目标列表", "按计划名自动建列表；也接受用户自定义标题",
     "配置项 auto_create_list（默认 true）+ default_list。导入时先生成计划名（用户显式标题 > 模型归纳的计划名 > default_list），再匹配或创建列表；一次导入只进一个列表"],
    ["提醒", "按用户指定的提醒时间，调用 AstrBot 内置定时任务创建提醒",
     "复用 context.cron_manager.add_active_job(job_type=active_agent)：run_once + run_at + timezone，payload 携带 session/sender_id/note；到点由 AstrBot 主动唤醒 Agent 并在原会话发消息。To Do 自身的 reminderDateTime 同时保留"],
    ["插件命名", "仓库保持 astrbot_plugins_todo，插件 name 用 astrbot_plugin_todo",
     "AstrBot 以目录名作为模块路径、以 metadata.name 作为 KV/配置命名空间；建议把仓库名或安装目录统一为 astrbot_plugin_todo，避免长期混淆"],
]

TOOL_HEADERS = ["阶段", "工具名", "参数", "返回", "说明"]
TOOL_ROWS = [
    ["M1", "ms_todo_account_status", "(无)", "文本", "查询绑定状态、账号、token 有效期"],
    ["M1", "ms_todo_login_start", "(无)", "文本（含授权网址与设备码）", "发起设备码授权；要求模型原样转达代码"],
    ["M1", "ms_todo_logout", "(无)", "文本", "解绑并删除本地凭据"],
    ["M1", "ms_todo_list_task_lists", "(无)", "文本", "列出 To Do 列表；连通性自检"],
    ["M2", "ms_todo_import_tasks", "tasks[], list_name?, confirmed, plan_id?", "文本（预览或写入结果）", "核心工具：两阶段提交，支持按计划名建列表"],
    ["M4", "ms_todo_list_tasks", "scope(today/overdue/upcoming/all), list_name?, limit?", "文本", "查询待办"],
    ["M4", "ms_todo_update_task", "task_id|title, due_date?, importance?, status?", "文本", "改期/改优先级/改状态"],
    ["M4", "ms_todo_complete_task", "task_id|title", "文本", "标记完成"],
    ["M4", "ms_todo_delete_task", "task_id|title", "文本", "删除（二次确认）"],
    ["M4", "ms_todo_add_checklist_items", "task_id|title, items[]", "文本", "给任务加子步骤"],
]

RISK_HEADERS = ["风险", "影响", "对策"]
RISK_ROWS = [
    ["To Do API 只支持委派权限（Application 为 Not supported）", "无法无人值守写入，必须人工授权一次",
     "设备码流程 + refresh_token 自动续期（官方默认 90 天）；失效时明确提示重新 /todo login"],
    ["refresh_token 可能被提前撤销（改密、管理员操作、单点注销）", "静默失败", "刷新失败即删除本地凭据并抛 AuthRequiredError，主动提示用户重新授权"],
    ["世纪互联（21Vianet）版 Microsoft 365 不支持 To Do API", "该租户用户完全不可用",
     "403 时给出明确文案；README 与文档提前声明，避免无意义重试"],
    ["企业网络无法直连 login.microsoftonline.com / graph.microsoft.com", "授权与调用超时", "提供 proxy 配置项；错误分类提示网络异常而非授权失败"],
    ["模型幻觉出错误日期或多余任务", "污染用户真实待办", "两阶段提交 + JSON Schema 强约束 + 单次导入上限 + source_key 去重"],
    ["群聊里多人共用一个机器人", "互相看到/覆盖凭据", "凭据按「平台:发送者ID」隔离；默认写入前预览确认；allow_any_user / allowed_users 可限流"],
    ["AstrBot#8578：插件 reload 后工具被静默置为 inactive", "工具消失且无报错", "注册后把 handler_module_path 对齐到插件主模块路径（已实现）"],
    ["Skills 需要开启「使用电脑能力」且人格未禁用 Skills", "Skill 不生效", "Skill 设计为纯说明书、零脚本，不依赖代码执行；README 说明前置条件"],
    ["Microsoft 正把任务体验向 Planner/Copilot 迁移", "长期 API 风险", "已确认 To Do（v1.0 todoTask）未被弃用；beta baseTask 早已停用。封装集中在 graph/client.py，便于将来切换端点"],
]

MILESTONE_HEADERS = ["里程碑", "名称", "目标", "状态", "预估合计(人时)"]
MILESTONE_ROWS = [
    ["M1", "骨架与授权闭环", "每个用户能各自完成绑定，凭据可持久化并自动续期", "已完成（已验收）", ""],
    ["M2", "导入链路", "自然语言 → 预览 → 确认 → 写入指定列表", "已完成（待真机验收）", ""],
    ["M3", "Skill", "Agent 按操作手册稳定完成抽取与确认流程", "待开始", ""],
    ["M4", "查询、管理与 WebUI", "增删改查 + WebUI 绑定管理页 + 批量与幂等", "待开始", ""],
    ["M5", "定时提醒", "复用 AstrBot 内置定时任务创建提醒", "待开始", ""],
    ["M6", "测试与发布", "单测/端到端/规范/文档/发布", "待开始", ""],
]


# ---------------------------------------------------------------- 构建

def _write_sheet(
    wb: Workbook,
    title: str,
    headers: list[str],
    rows: list[list[str]],
    widths: list[int],
    *,
    status_col: int | None = None,
    freeze: str = "A3",
    note: str = "",
) -> None:
    ws = wb.create_sheet(title)
    ws["A1"] = f"{title}（生成时间 {datetime.now():%Y-%m-%d %H:%M}）"
    ws["A1"].font = TITLE_FONT
    if note:
        ws.cell(row=1, column=2, value=note).font = Font(size=9, color="808080")

    header_row = 2
    for idx, head in enumerate(headers, start=1):
        cell = ws.cell(row=header_row, column=idx, value=head)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = CENTER
        cell.border = BORDER

    for r, row in enumerate(rows, start=header_row + 1):
        for c, value in enumerate(row, start=1):
            cell = ws.cell(row=r, column=c, value=value)
            cell.alignment = WRAP
            cell.border = BORDER
            if c == 1:
                cell.fill = MILESTONE_FILL
            if status_col and c == status_col:
                fill = STATUS_FILL.get(str(value))
                if fill:
                    cell.fill = fill

    for idx, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = width
    ws.freeze_panes = freeze
    ws.auto_filter.ref = f"A{header_row}:{get_column_letter(len(headers))}{header_row + len(rows)}"
    ws.row_dimensions[header_row].height = 20


def build(output: Path) -> Path:
    wb = Workbook()
    wb.remove(wb.active)

    _write_sheet(wb, "开发计划", PLAN_HEADERS, PLAN_ROWS,
                 [26, 7, 34, 74, 34, 14, 10, 11], status_col=7,
                 note="状态：已完成 / 待验收 / 进行中 / 待开始")

    # 里程碑合计预估
    totals: dict[str, float] = {}
    for row in PLAN_ROWS:
        try:
            totals[row[0]] = totals.get(row[0], 0.0) + float(row[7])
        except (TypeError, ValueError):
            continue

    milestone_headers = MILESTONE_HEADERS
    milestone_rows = []
    for m, name, goal, status, _ in MILESTONE_ROWS:
        key = next((k for k in totals if k.startswith(m + " ")), None)
        milestone_rows.append([m, name, goal, status, totals.get(key, 0.0) if key else 0.0])
    _write_sheet(wb, "里程碑概览", milestone_headers, milestone_rows,
                 [10, 24, 56, 24, 16], status_col=4, freeze="A3")

    _write_sheet(wb, "决策记录", DECISION_HEADERS, DECISION_ROWS, [16, 40, 90], freeze="A3")
    _write_sheet(wb, "工具清单", TOOL_HEADERS, TOOL_ROWS, [8, 32, 40, 30, 46], freeze="A3")
    _write_sheet(wb, "配置项", ["配置", "类型", "默认", "说明"], CONFIG_ROWS, [20, 10, 26, 74], freeze="A3")
    _write_sheet(wb, "风险与对策", RISK_HEADERS, RISK_ROWS, [42, 30, 76], freeze="A3")

    # 总工时
    ws = wb["里程碑概览"]
    total = sum(v for v in totals.values())
    last = 3 + len(milestone_rows)
    ws.cell(row=last, column=2, value="合计").font = Font(bold=True)
    cell = ws.cell(row=last, column=5, value=total)
    cell.font = Font(bold=True)
    cell.number_format = "0.0"
    for col in range(1, 6):
        ws.cell(row=last, column=col).border = BORDER

    wb.save(output)
    return output


CONFIG_ROWS = [
    ["client_id", "string", "（空）", "必填。Entra 应用的应用程序(客户端) ID"],
    ["client_secret", "string(secret)", "（空）", "仅机密客户端需要；公共客户端留空"],
    ["tenant", "string", "common", "common / consumers / organizations"],
    ["scope", "string", "offline_access Tasks.ReadWrite User.Read", "OAuth 权限范围，保持默认即可"],
    ["timezone", "string", "Asia/Shanghai", "解释相对时间的基准时区"],
    ["default_list", "string", "AstrBot 待办", "未指定计划名时的目标列表"],
    ["auto_create_list", "bool", "true", "按计划名自动创建列表"],
    ["require_confirm", "bool", "true", "写入前先预览并等待用户确认"],
    ["max_tasks_per_import", "int", "20", "单次导入任务数上限"],
    ["reminder_enabled", "bool", "true", "按用户指定时间创建 AstrBot 定时提醒"],
    ["allow_any_user", "bool", "true", "关闭后仅白名单用户可用"],
    ["allowed_users", "list", "[]", "用户 ID 白名单"],
    ["request_timeout", "int", "30", "HTTP 超时（秒）"],
    ["proxy", "string", "（空）", "企业网络代理，如 http://127.0.0.1:7890"],
]


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "开发计划.xlsx"
    path = build(out)
    print(f"已生成: {path}")
    print(f"工作表: 开发计划 / 里程碑概览 / 决策记录 / 工具清单 / 配置项 / 风险与对策")
    print(f"任务行数: 主计划 {len(PLAN_ROWS)} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
