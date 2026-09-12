# astrbot_plugin_todo（待办事项 · Microsoft To Do）

把「接下来要办的事 / 计划」用自然语言丢给机器人，AI 整理成结构化待办清单，确认后写入
你**自己的** Microsoft To Do。

> 当前进度：**M1（账号绑定与连通性）、M2（导入链路：预览 → 确认 → 写入）已完成**。
> M3 起补 Skill 说明书与 AstrBot 定时提醒，详见仓库根目录的 `开发计划.xlsx`。

## 特性

- **每个用户各自绑定**：凭据按「平台 + 用户」隔离存储，群聊里互不干扰。
- **无需在服务器上装浏览器**：使用 OAuth 2.0 设备码流程（Device Code Flow），
  在手机或任意电脑的浏览器上完成一次授权即可，不需要公网回调地址、不需要内网穿透。
- **自动续期**：refresh_token 自动滚动刷新（官方默认 90 天），
  失效时给出明确提示而不是静默失败。
- **写入前先预览**：模型整理出的清单会先发给你确认，确认后才写入 To Do，避免误写真实数据。
- **按计划名自动建列表**：说「下个月搬家要办的事」，就写进「搬家」列表；也能指定已有列表。
- **幂等**：同一批待办重复导入会自动跳过，不会在 To Do 里堆重复项。去重同时看**本地记录**和
  **To Do 里的现有任务**，所以换机器、重装 AstrBot、甚至你自己在手机上手动建过同一条，
  都不会重复导入。
- **批量写入**：一次导入多条时走 Graph 的 `$batch`（每批 ≤20），把 N 次请求压成 1 次。
- **工具调用 + 指令双通道**：模型支持函数调用时走 Tool；同时提供 `/todo` 指令兜底
  （指令与工具共用同一套业务逻辑）。

## 安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/MR-HATE/astrbot_plugins_todo.git astrbot_plugin_todo
```

> 目录名建议保持 `astrbot_plugin_todo`，与 `metadata.yaml` 里的 `name` 一致。
> 依赖只有 `httpx`（AstrBot 已内置），一般无需额外安装。
> 修改代码后在 WebUI 插件页点「重载插件」即可生效。

## 配置 Entra 应用（一次性，约 3 分钟）

Microsoft To Do 的 Graph API **只支持委派权限**，因此必须注册一个应用并让用户授权一次。

1. 打开 [Microsoft Entra 管理中心 → 应用注册](https://entra.microsoft.com/) → 「新注册」。
2. 名称随意（如 `astrbot-todo`）；**受支持的账户类型**选
   「任何组织目录中的账户和个人 Microsoft 账户」。
3. **重定向 URI 留空**（设备码流程不需要回调地址）。
4. 注册完成后，在「身份验证」页把 **「允许公共客户端流」设为「是」**（设备码流程必需）。
5. 在「API 权限」→「添加权限」→「Microsoft Graph」→「委托的权限」中添加：
   - `Tasks.ReadWrite`
   - `offline_access`（若列表中没有，保持默认 scope 即可，插件已默认请求）
   - `User.Read`（用于显示绑定的是哪个账号）
6. 复制「应用程序(客户端) ID」，填入 AstrBot 插件配置的 `client_id`。

> 工作/学校账号可能需要在租户中由管理员授予同意。
> **世纪互联（21Vianet）运营的 Microsoft 365 不支持 To Do API**，无法使用本插件。

## 使用

### 1. 绑定账号（一次性）

```
/todo login    绑定 / 重新绑定（返回授权网址与设备码）
/todo status   查看绑定状态与凭据有效期
/todo lists    列出已有列表（同时验证连通性）
/todo logout   解绑并删除本机凭据
```

授权流程（不需要在 AstrBot 机器上做任何事）：

```
你: /todo login
机器人: 请在 15 分钟内完成 Microsoft 授权：
        1. 用手机或电脑浏览器打开：https://microsoft.com/devicelogin
        2. 输入代码：XXXXXXXXX
        3. 登录并同意「读取和写入任务」权限
（你在手机上完成）
机器人: ✅ Microsoft To Do 绑定成功：张三 <zhangsan@outlook.com>
```

### 2. 导入计划

直接对机器人说人话即可（模型会自动调用工具）：

```
你: 下周要去上海出差，周三前订好酒店和机票，出发前一天提醒我带身份证
机器人: 📋 待办预览 · 目标列表「上海出差」 · 共 3 项
        1. 订好酒店和机票
           截止：2026-09-16（周三）
        2. 收拾行李
           截止：2026-09-15（周二）
        3. 提前值机
           截止：2026-09-15（周二） 20:00
        回复「确认」即可导入；回复「取消」放弃这次清单。
你: 确认
机器人: ✅ 已写入 Microsoft To Do · 列表「上海出差」（成功 3 项）
```

模型不支持函数调用时，用指令走同一条链路：

```
/todo import 下周三前订好酒店，出发前一天提醒我带身份证
/todo confirm   确认并写入上一次预览
/todo cancel    放弃上一次预览
```

> **写入策略**：默认「预览 → 确认 → 写入」。如果你更希望直接写入，把配置项
> `require_confirm` 关掉即可（模型整理完直接落库）。
> 预览有 30 分钟有效期，超时需要重新生成，避免隔天误确认。

### 3. 查询待办与完成情况

直接问即可（模型调用 `ms_todo_list_tasks`，**只读**）：

```
你: 今天还有什么要做的？
机器人: 📋 待办 · 全部列表 · 未完成 · 今天到期或已逾期 · 共 3 项

        ⬜ 补交季度报告
           截止 已逾期 2 天 · 2026-09-10（周四） 09:00 · 列表「工作」
        ⬜ 🔴 交房租
           截止 今天 · 2026-09-12（周六） 18:00 · 列表「工作」
        ⬜ 买菜
           截止 今天 · 2026-09-12（周六） 00:00 · 列表「生活」

你: 上周的事都完成了吗？
机器人: 📋 待办 · 全部列表 · 已完成 · 共 1 项

        ✅ 已完成的报销
           截止 2026-09-11（周五） 09:00 · 完成于 2026-09-11 14:20 · 列表「工作」
```

查询范围 `scope`：`pending`（默认，未完成且今天到期或逾期）、`today`、`overdue`、
`upcoming`（未来 7 天）、`completed`、`all`。也可以指定 `list_name` 只看某个列表。
状态标记：`⬜` 未完成 · `🔄` 进行中 · `✅` 已完成 · `⏳` 等待他人 · `💤` 已推迟。

指令兜底：`/todo today`。

### 4. 到点提醒

说「提醒我」时，除了写入 To Do 自己的提醒，**机器人还会在原会话里主动提醒一次**
（通过 AstrBot 内置的定时任务，不需要额外的轮询进程）：

```
你: 明天下午三点提醒我给客户回电话
机器人: 📋 待办预览 … 1. 给客户回电话  提醒：2026-09-13 15:00
你: 确认
机器人: ✅ 已写入 … ⏰ 已为 1 项创建到点提醒，可在 WebUI 的「未来任务」里查看或取消。
（到点后）
机器人: 到点啦，记得给客户回电话。
```

重复提醒也支持：

| 你说 | 效果 |
| --- | --- |
| 每天早上 8 点提醒我记账 | 每天 08:00 |
| 每周五下午 3 点提醒我写周报 | 每周五 15:00 |
| 每月 10 号提醒我交房租 | 每月 10 日 09:00 |

细节：

- 任务被**标记完成**后，它的提醒会**自动取消**；删除任务/列表时同样会清理；
- 创建的定时任务在 WebUI 左侧「**未来任务**」页面可以看到、也能手动删改；
- 平台不支持机器人主动发消息时（如 QQ 官方接口），提醒不会创建，结果里会明确说明——
  这时只有 To Do 客户端自己的提醒生效；
- 想彻底关掉可以用配置项 `reminder_enabled`。

## 指令与工具对照

| 指令 | LLM 工具 | 说明 |
| --- | --- | --- |
| `/todo login` | `ms_todo_login_start` | 发起设备码授权 |
| `/todo status` | `ms_todo_account_status` | 查询绑定状态 |
| `/todo logout` | `ms_todo_logout` | 解绑 |
| `/todo lists` | `ms_todo_list_task_lists` | 列出待办列表 |
| `/todo today` | `ms_todo_list_tasks` | 读取今天未完成（含逾期）的待办及完成状态 |
| `/todo import <计划>` | `ms_todo_import_tasks` | 整理成待办并生成预览（`confirmed=false`）或直接写入 |
| `/todo confirm` | `ms_todo_confirm_import` / `ms_todo_delete_task` / `ms_todo_delete_list` | 确认预览写入；若刚发起删除，则确认执行删除 |
| `/todo cancel` | `ms_todo_confirm_import` | 放弃预览或取消待确认的删除（`action=cancel`） |
| `/todo done <关键词>` | `ms_todo_complete_task` | 把匹配的待办标记为已完成 |
| `/todo del <关键词>` | `ms_todo_delete_task` | 删除一条待办（需再发一次 `/todo confirm`） |
| `/todo dellist <列表名>` | `ms_todo_delete_list` | 删除整个列表及其中任务（需再发一次 `/todo confirm`） |
| （对话即可） | `ms_todo_update_task` | 改期 / 改优先级 / 改标题 / 改备注 / 清除截止时间 |
| （对话即可） | `ms_todo_add_checklist_items` | 给已有待办追加子步骤 |

改动类操作都能直接说人话，例如：

```
你: 把交房租改到周五
机器人: ✅ 已修改「交房租」：- 截止改为 2026-09-18 18:00

你: 交房租做完了
机器人: ✅ 「交房租」已完成。

你: 删掉买菜那条
机器人: ⚠️ 即将删除 1 项，删除后无法恢复：
        - 买菜 · 列表「生活」
        请确认；确认后才会真正删除。
你: 确认
机器人: 🗑 已删除 1 项：买菜

你: 把上海出差那个列表删掉
机器人: ⚠️ 即将删除整个列表「上海出差」，该列表下有 3 个任务（其中 2 个未完成）。
        列表和里面的任务会一起消失，且无法恢复。
        请确认；确认后才会真正删除。
```

> 匹配到多条时（比如"写周报"和"写季度报告"都含"写"），工具会返回候选清单并让你选，
> 不会自己挑一个改。删列表会连带删掉里面所有任务，所以确认信息里会先告诉你数量。
> Microsoft To Do 的**默认列表不能删除**。

## Skill：让模型更守规矩（可选增强）

插件内置了一个 Skill：**`ms-todo-import`**（见 `skills/ms-todo-import/`）。
加载插件后它会自动出现在 WebUI 的 **Skills** 页面，来源显示为插件、**只读**（可启用/停用，
不能在本机 Skills 页编辑或删除）。

它给模型一份操作手册，管的是**工具描述管不了的事**：

- 什么时候该触发、什么时候该先追问而不是硬猜；
- 拆分粒度（一件事 = 一个任务，子步骤用 `steps`，别把一个动作拆成十个任务）；
- 时间换算规则与"没有时间就不要编日期"；
- **必须先出预览、等用户确认再写入**；
- 预览里出现「⚠️ 需要留意」时必须转达用户；
- 目标列表的选择优先级、异常话术、反例清单。

> ⚠️ **前置条件**：AstrBot 的 Skill 机制里，系统提示词只内联每个 Skill 的 **name + description**，
> 正文 `SKILL.md` 需要 Agent 用 shell 命令读取。因此要在 WebUI
> **配置 → 使用电脑能力** 里把运行环境设为 `Local` 或 `Sandbox`，Skill 才会真正生效。
> Local 模式下只有 AstrBot **管理员**能执行命令，普通群友只能看到描述。

### 规则兜底注入（无需启用电脑能力）

为了不依赖上面那个前置条件，插件还会通过 `on_llm_request` 钩子把**精简版规则**直接追加到
system prompt（配置项 `inject_rules`，默认开启）：

1. 先预览、后写入（`confirmed` 一律留空）；
2. 不编造日期，模糊时间留空并说明；
3. 拆分粒度（一件事 = 一个任务，子步骤进 `steps`）；
4. 别误触发，意图不明先追问；
5. 如实回报警告与失败，不假装成功；
6. 说明当前只支持导入与查看列表。

这样的分工是：

| 机制 | 作用 | 生效条件 |
| --- | --- | --- |
| **工具**（`ms_todo_*`） | 功能本身：授权、预览、写入 | 只要有 `client_id` 就能用 |
| **规则注入**（钩子） | 兜底约束模型行为 | 默认开启，所有用户、所有环境 |
| **Skill**（`SKILL.md`） | 完整操作手册，含拆分/时间/反例细节 | 需启用「使用电脑能力」，且 Local 下需管理员 |

注入内容**固定不变**，不会破坏服务端提示词缓存；同一轮工具循环里也只追加一次。
如果不想要这段额外 token（约 300），把 `inject_rules` 关掉即可。

所以：Skill 是"让模型表现更好"的增强项，工具是"功能可用"的必需项。

## 配置项

见 AstrBot WebUI 插件配置页（`_conf_schema.json` 内每项都带说明）。常用的：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `client_id` | 空 | **必填**，Entra 应用 ID |
| `tenant` | `common` | 支持个人账号 + 工作/学校账号 |
| `timezone` | `Asia/Shanghai` | 解释「明天」「下周三」的基准时区 |
| `default_list` | `AstrBot 待办` | 未指定计划名时的目标列表 |
| `auto_create_list` | 开 | 按计划名自动创建列表 |
| `require_confirm` | 开 | 写入前先预览、等用户确认 |
| `reminder_enabled` | 开 | 按用户指定时间创建 AstrBot 定时提醒 |
| `allow_any_user` | 开 | 关闭后仅白名单用户可用 |
| `proxy` | 空 | 企业网络需要代理时填写 |

## 常见问题

**Q：机器人说「还没有绑定 Microsoft 账号」，但我明明绑过了。**
账号是按「**平台 + 用户**」隔离的（`平台名:发送者ID`）。在 QQ 里绑定的账号，在 AstrBot
WebUI 的 Chat 页里不算数——那是另一个"用户"。要在哪用就在哪 `/todo login` 一次。

**Q：任务没写进我以为的那个列表。**
目标列表按 `plan_name` 自动创建/复用。如果模型把这次计划归纳成「出差」，任务就会进
「出差」列表（可能新建）。用 `/todo lists` 看全部列表，或 `/todo today` 跨列表查。

**Q：说「提醒我」，但没收到提醒。**
按顺序检查：
1. 回执里有没有「⏰ 已为 N 项创建…」——没有的话，回执会说明原因；
2. 平台是否支持主动消息（QQ 官方接口、WebUI Chat 等**不支持**）；
3. WebUI 左侧「未来任务」页有没有那条 `待办提醒 · XXX`；
4. 配置项 `reminder_enabled` 是不是被关了。

**Q：机器人答应了但什么都没发生。**
大概率是模型**没有真正调用工具**（只是嘴上答应了）。插件无法阻止这种情况，但可以：
换用支持 function calling 的模型，或直接用指令——`/todo import <计划>` 一定会走管线。

**Q：改了代码但行为没变。**
AstrBot 插件是运行时注入的，改完要在插件管理页点「**重载插件**」，刷新浏览器没用。
另外确认代码确实同步到了运行 AstrBot 的那台机器（日志里搜 `已注册 12 个 LLM 工具`）。

**Q：能不能只删任务、不删列表？**
`/todo del <关键词>` 删单条任务；`/todo dellist <列表名>` 才删整个列表。两者都要二次确认。

## 隐私与安全

- 只申请 `Tasks.ReadWrite` / `User.Read` 委派权限，**只能访问授权账号自己的待办**。
- 凭据保存在 AstrBot 的插件级 KV 存储中，按 `作者/插件名` 命名空间隔离；
  日志中不会输出 token 内容。
- `/todo logout` 会立即删除本地凭据。如果怀疑泄露，请同时到
  [Microsoft 账户 → 应用权限](https://account.live.com/consent/Manage) 撤销授权。

## 开发

```bash
# 目录结构
main.py                     插件入口：指令 + 工具注册 + 业务逻辑
graph/auth.py               OAuth 设备码授权 / token 续期
graph/client.py             Graph REST 封装（重试、限流、批量、错误分类）
graph/planner.py            时间归一化 / 校验 / 去重 / 渲染 / crontab 生成
graph/models.py             数据模型与函数调用 Schema
graph/store.py              基于插件 KV 的凭据存储
tools/todo_tools.py         FunctionTool 定义（LLM 函数调用）
skills/ms-todo-import/      插件内置 Skill（Agent 操作手册）
tests/                      单元测试 + 手动验收清单
scripts/                    开发计划 Excel 生成脚本、规范自检
```

### 跑测试

```bash
python tests/run_tests.py          # 全部（90 个用例，零依赖）
python tests/run_tests.py planner  # 只跑文件名含 planner 的
pytest tests/                      # 装了 pytest 也能直接跑（无需 pytest-asyncio）
```

测试用假 Graph 客户端 + 假定时任务 + 内存 KV，**不会碰你的真实 To Do**。

### 代码规范

```bash
python scripts/lint_check.py   # 零依赖自检：未使用 import / 行宽 / 行尾空白 / 裸 except
ruff check . && ruff format .   # 装了 ruff 的话以它为准
```

- 行宽上限 100；`scripts/build_dev_plan.py` 用文件头的 `# lint: max-line=400` 豁免
  （它是一张"一行一条数据"的表）。
- 遵循 AstrBot 插件开发规范：异步 `httpx`、持久化数据放插件级 KV、不引入 `requests`。

### 发版前

过一遍 [`tests/manual-checklist.md`](tests/manual-checklist.md)——单元测试用假客户端，
覆盖不到真实授权、真实 To Do 界面和到点提醒。

- 灵感来源：Anthropic Agent Skills 规范、AstrBot 官方插件模板。

## 许可证

AGPL-3.0（继承自 AstrBot 插件模板）。
