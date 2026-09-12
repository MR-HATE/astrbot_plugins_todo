# astrbot_plugin_todo · 待办事项（Microsoft To Do）

把「接下来要办的事」用一句自然语言丢给机器人，AI 整理成结构化待办清单，**确认后**写入
你**自己的** Microsoft To Do，并在到点时由机器人在原会话主动提醒。

```
你：下周去上海出差，周三前订好酒店和机票，出发前一晚 8 点提醒我收拾行李
机器人：（预览）📋「上海出差」共 2 项 …… 回复「确认」即写入
你：确认
机器人：✅ 已写入，打开 To Do 就能看到
```

- 插件标识：`astrbot_plugin_todo`
- 作者：Mr_Hate　|　版本：v1.0.0　|　许可：AGPL-3.0
- 仓库：https://github.com/MR-HATE/astrbot_plugins_todo

---

## 一、项目简介

### 它解决什么问题

Microsoft To Do 好用，但「把脑子里那堆事一条条敲进 App」很烦。这个插件让你直接用说话的
方式记待办——机器人在聊天里帮你拆分、算日期、排优先级，落到你真实的 To Do 账号里；
到了时间还会回来提醒你。

### 核心能力

| 能力 | 说明 |
| --- | --- |
| 🔐 **设备码授权** | 服务器**无需浏览器、无需公网回调、无需内网穿透**。用手机打开链接输个码就绑定好了 |
| 👤 **每个用户各自绑定** | 凭据按「平台 + 发送者 ID」隔离存在 AstrBot 插件 KV 里，群里互不干扰 |
| 🔁 **自动续期** | access_token 到期前静默续期；refresh_token 每次刷新后覆盖保存，失效时明确提示重新授权 |
| 📝 **两阶段导入** | 先出**预览**、你回复确认后才真正写入。To Do 没有服务端回滚，所以默认不替你拍板 |
| 🗓️ **时间归一化** | `明天` / `下周三` / `三天后` / `9月18日` / `12-01`，以及 `下午三点`、`九点半` 这类中文时刻 |
| 🎯 **优先级识别** | `重要` / `紧急` / `不急` / `urgent` 等口语映射到 high / normal / low |
| ✅ **子步骤** | 一件事的多个步骤写进 To Do 的「步骤」（checklist），而不是拆成一堆任务 |
| 🔍 **查询与完成状态** | 问「今天还有什么要做的」「上周的事做完没」，读的是**真实状态**（⬜🔄✅⏳💤） |
| ✏️ **增删改** | 改期、改优先级、改标题、追加子步骤、勾完成、删任务、删整个列表 |
| ⏰ **到点主动提醒** | 复用 AstrBot 内置定时任务，到点在原会话里提醒；重复提醒走 crontab |
| 🧠 **跨机器幂等** | 去重同时看本地记录与 To Do 现有任务——换机器、重装、甚至你手动建过同一条都不会重复导入 |
| 🤖 **双通道** | Agent 支持函数调用时走 12 个 LLM 工具；同时也提供 `/todo` 指令兜底 |

### 怎么用（两种方式）

**1. 直接说话**（需要模型支持函数调用，推荐）

> 「帮我记一下：周五之前把季度报告交了，重要」
>
> 「今天还有什么要做的？」
>
> 「把交房租改到周五」
>
> 「上海出差那个列表删了吧」

模型会调用 `ms_todo_*` 工具，先给预览、等你确认。仓库内置 Skill
[`skills/ms-todo-import`](skills/ms-todo-import/SKILL.md) 约束它的行为。

**2. 用指令**（不依赖模型，随时可用）

| 指令 | 作用 |
| --- | --- |
| `/todo login` | 绑定 / 重新绑定 Microsoft 账号（返回授权网址和设备码） |
| `/todo status` | 查看绑定状态与凭据有效期 |
| `/todo lists` | 列出已有列表（顺带验证连通性） |
| `/todo logout` | 解绑并删除本机保存的凭据 |
| `/todo today` | 查看今天还没做完的（含已逾期） |
| `/todo import <一段话>` | 把一段计划整理成待办并生成预览 |
| `/todo confirm` | 确认写入上一次的预览（若刚发起过删除，则确认删除） |
| `/todo cancel` | 放弃上一次的预览 |
| `/todo done <关键词>` | 把匹配的待办标记为已完成 |
| `/todo del <关键词>` | 删除一条待办（需再发一次 `/todo confirm`） |
| `/todo dellist <列表名>` | 删除整个列表（需再发一次 `/todo confirm`） |

### 目录结构

```
main.py                     插件入口：生命周期、业务逻辑、/todo 指令、提示词兜底
metadata.yaml               插件元信息（市场展示用）
_conf_schema.json           WebUI 插件配置项定义
requirements.txt            依赖（仅 httpx，AstrBot 已内置）
graph/                      Microsoft Graph 接入层
  ├─ auth.py                OAuth 2.0 设备码流程 + token 续期
  ├─ client.py              Graph REST 薄封装（重试 / 限流 / 错误分类 / $batch）
  ├─ models.py              待办数据模型与函数调用 Schema
  ├─ planner.py             时间归一化 / 校验 / 去重 / 预览渲染
  ├─ store.py               基于插件 KV 的凭据存储
  └─ errors.py              统一异常类型
tools/todo_tools.py         12 个 LLM 工具的 Schema 定义
skills/ms-todo-import/      给 Agent 看的 Skill 说明书（含时间规则与正反例）
tests/                      单元测试与手动验收清单
scripts/lint_check.py       零依赖代码规范自检
```

---

## 二、安装方法

### 前置条件

- **AstrBot ≥ 4.17.0**（`metadata.yaml` 中已声明；定时提醒相关接口按 4.28 源码核对）
- 一个**能访问 `login.microsoftonline.com` 和 `graph.microsoft.com` 的网络环境**
  （服务器在国内的话可能需要代理，见配置项 `proxy`）
- 一个 Microsoft 账号（个人账号或工作/学校账号均可）

### 方式一：WebUI 插件市场（推荐）

1. 打开 AstrBot WebUI → 左侧「**插件市场**」；
2. 搜索 `待办` 或 `Microsoft To Do`，找到本插件；
3. 点「安装」，然后在「插件管理」页确认它已启用；
4. 按下面的[第三节](#三配置方法)填写配置，再点一次「**重载插件**」。

### 方式二：手动安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/MR-HATE/astrbot_plugins_todo.git astrbot_plugin_todo
```

> 目录名请保持 `astrbot_plugin_todo`，与 `metadata.yaml` 里的 `name` 一致。

### 安装依赖

依赖只有 `httpx`，**AstrBot 已内置**，通常不需要额外操作。若你的环境确实缺：

```bash
pip install -r requirements.txt
```

### 验证安装

重载插件后，日志里应出现：

```
astrbot_plugin_todo 已就绪（tenant=common, 时区=Asia/Shanghai）
已注册 12 个 LLM 工具: ms_todo_account_status, ms_todo_login_start, ...
```

在任意会话里发 `/todo status`，能收到回复就说明插件加载正常（此时还没绑定账号，
回复「尚未绑定 Microsoft 账号」是**正常**的）。

### 更新

```bash
cd AstrBot/data/plugins/astrbot_plugin_todo
git pull
```

然后在 WebUI 点「**重载插件**」——**只刷新浏览器是不够的**。

---

## 三、配置方法

配置分两步：先在 Microsoft Entra 注册一个应用（一次性，约 3 分钟），再把 ID 填进插件配置。

### 3.1 注册 Entra 应用（一次性）

Microsoft To Do 的 Graph API **只支持委派权限（Delegated）**，不支持 Application 权限，
所以无法无人值守写入，必须注册应用并让每个用户授权一次。

1. 打开 [Microsoft Entra 管理中心 → 应用注册](https://entra.microsoft.com/) → 「**新注册**」；
2. 名称随意（例如 `astrbot-todo`）；**受支持的账户类型**选
   「**任何组织目录中的账户和个人 Microsoft 账户**」；
3. **重定向 URI 留空**——设备码流程不需要回调地址；
4. 注册完成后进入「**身份验证**」页，把「**允许公共客户端流**」设为「**是**」
   （设备码流程必需，忘了这步授权会失败）；
5. 进入「**API 权限**」→「添加权限」→「Microsoft Graph」→「**委托的权限**」，添加：
   - `Tasks.ReadWrite` —— 读写待办（必需）
   - `User.Read` —— 显示绑定的是哪个账号
   - `offline_access` —— 如果列表里找不到就跳过，插件默认 scope 里已请求
6. 复制概览页的「**应用程序(客户端) ID**」，填入下一步的 `client_id`。

> 💡 若用「机密客户端」（填了 `client_secret`），还需要在「证书和密码」里新建一个客户端密码。
> 设备码流程用**公共客户端**即可，不必多这一步。

### 3.2 填写插件配置

WebUI →「插件管理」→ 找到本插件 →「**配置**」：

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `client_id` | string | 空 | **必填**。上一步拿到的「应用程序(客户端) ID」 |
| `client_secret` | string | 空 | 可选。仅「机密客户端」需要；公共客户端留空 |
| `tenant` | string | `common` | `common` 支持个人 + 工作/学校账号；`consumers` 仅个人；`organizations` 仅工作/学校 |
| `scope` | string | `offline_access Tasks.ReadWrite User.Read` | 保持默认即可 |
| `timezone` | string | `Asia/Shanghai` | 解释「明天」「下周三」的基准时区（IANA 名称） |
| `default_list` | string | `AstrBot 待办` | 用户没指定计划名时导入到哪个列表；不存在会自动创建 |
| `auto_create_list` | bool | `true` | 按计划名自动创建/复用列表；关闭则统一进默认列表 |
| `require_confirm` | bool | `true` | 写入前先出预览等确认。**建议保持开启** |
| `max_tasks_per_import` | int | `20` | 单次导入任务数上限，超出会要求分批 |
| `inject_rules` | bool | `true` | 把关键待办规则追加到 system prompt（未启用「使用电脑能力」时的兜底） |
| `reminder_enabled` | bool | `true` | 启用 AstrBot 定时提醒 |
| `allow_any_user` | bool | `true` | 关闭后只有白名单用户能**发起绑定、导入、增删改和提醒** |
| `allowed_users` | list | `[]` | 用户 ID 白名单，仅在 `allow_any_user=false` 时生效 |
| `request_timeout` | int | `30` | 访问 Microsoft 接口的超时（秒） |
| `proxy` | string | 空 | 例如 `http://127.0.0.1:7890`，仅企业网络需要 |

改完配置记得点「**重载插件**」。

### 3.3 绑定账号

在**你要用的那个平台**的会话里发送：

```
/todo login
```

机器人会返回一个网址和一个设备码。用**手机或任意电脑**的浏览器打开网址、输入设备码、
登录并同意「读取和写入任务」权限。完成后机器人会在**原会话**主动通知你绑定成功。

> 授权流程有效期 15 分钟，超时重新发 `/todo login` 即可。

发 `/todo status` 确认：应显示绑定的账号、access_token 剩余时间和 refresh_token 已保存。

---

## 四、注意事项

### ⚠️ 必读

1. **必须自己注册 Entra 应用并填 `client_id`**。本插件不自带应用 ID，`client_id` 为空时所有
   待办功能都会返回「尚未配置 client_id」。
2. **别忘了开「允许公共客户端流」**。这是设备码流程的硬性要求，漏了会授权失败。
3. **世纪互联（21Vianet）运营的 Microsoft 365 不支持 To Do API**，无法使用本插件。
   国际版 Microsoft 365 与个人 Microsoft 账号正常。
4. **账号按「平台 + 用户」隔离**。在 QQ 里绑的账号，在 WebUI 的 Chat 页**不算数**，
   需要各自绑一次。
5. **删除不可恢复**。删任务和删列表都是两阶段确认，请认真看第一次返回的「将删除什么」。
   Microsoft To Do 的**默认列表不能删除**（工具会拒绝并说明原因）。
6. **写入前默认要确认**。`require_confirm` 打开时，预览阶段不会写入任何数据。
   关掉它等于让模型直接落库，误写的代价请自行评估。
7. **白名单只约束写操作**。`allow_any_user=false` 时，查看绑定状态（`/todo status`）、
   解绑、查看列表和读取待办仍然对所有人开放——这些是只读/自清理操作，
   卡住它们反而会让用户没法自查和撤销授权。

### 平台能力差异

- **能主动推送提醒**的平台：`aiocqhttp`、`telegram`、`discord`、`slack`、`lark`、`misskey`、`satori`。
  其他平台（如 WebUI 的 Chat 页、QQ 官方接口）**任务照常写入 To Do，但不会创建定时提醒**，
  插件会在回执里明确告诉你。
- `metadata.yaml` 里声明支持的平台是 `aiocqhttp` 和 `telegram`。
- 提醒任务可以在 WebUI 左侧「**未来任务**」页查看或手动取消。

### 功能边界

- **不支持重复任务**——「每天/每周」的重复只作用在**提醒**上，任务本身不会自动重建。
- 不支持移动任务到其它列表，不支持重命名列表。
- `remind_repeat` 的星期/日期是从 `remind_at` 推导的，所以「每周五」必须让 `remind_at`
  落在周五，否则会在错误的日子提醒（Skill 里有明确规则约束模型）。
- 时间解析依赖配置的 `timezone`，请按你所在地设置。

### 隐私与数据

- OAuth 凭据（含 refresh_token）保存在 **AstrBot 的插件级 KV**（`shared_preferences`）里，
  按 `{平台}:{发送者ID}` 隔离；插件不把凭据写到插件目录，也不上传到任何第三方。
- 待办内容会发给 Microsoft Graph API（这是它的本职工作）。
- 若模型支持函数调用，待办内容也会经过你配置的 **LLM 服务商**。
- 发 `/todo logout` 可随时删除本机凭据；建议同时到
  [Microsoft 账户的已授权应用](https://account.live.com/consent/Manage) 里撤销授权。

### 常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| 回复「尚未配置 client_id」 | 配置里没填 `client_id`，或填完没点「重载插件」 |
| 授权时报 `AADSTS7000218` | Entra 应用没开「允许公共客户端流」 |
| 授权报 `invalid_grant` / 提示重新授权 | refresh_token 失效（改密码、撤销授权、长期未用），重新 `/todo login` |
| 工具没被调用 | 模型不支持函数调用；改用 `/todo import` 指令 |
| 到点没收到提醒 | 平台不支持主动消息，或 `reminder_enabled` 关闭，或提醒时间已过 |
| 连不上 Microsoft | 服务器网络受限，配置 `proxy` |
| 提示 To Do API 不可用 | 用的是世纪互联版 Microsoft 365 |

### 开发者

```bash
python tests/run_tests.py            # 跑全部单元测试（零依赖，无需 pytest）
python tests/run_tests.py planner    # 只跑文件名含 planner 的
python scripts/lint_check.py         # 代码规范自检（装了 ruff 请以 ruff 为准）
```

发版前请按 [`tests/manual-checklist.md`](tests/manual-checklist.md) 在真机上过一遍
端到端验收（真实授权、真实界面、真实到点提醒——这些自动化测试覆盖不到）。

变更历史见 [`CHANGELOG.md`](CHANGELOG.md)。

> **开发说明**：本项目在开发过程中使用了DeepSeek V4.1-Flash和DeepSeek Harness参与代码编写、测试补齐与文档整理。所有功能均在真机上按
> [`tests/manual-checklist.md`](tests/manual-checklist.md) 验收通过，
> 代码与设计决策由维护者负责。

---

## 许可

[AGPL-3.0](LICENSE)
