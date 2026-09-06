# astrbot_plugin_todo（待办事项 · Microsoft To Do）

把「接下来要办的事 / 计划」用自然语言丢给机器人，AI 整理成结构化待办清单，确认后写入
你**自己的** Microsoft To Do。

> 当前进度：**M1（账号绑定与连通性）已完成**。M2 起补齐「自然语言 → 清单 → 预览确认 →
> 写入指定列表」的完整链路，详见仓库根目录的 `开发计划.xlsx`。

## 特性

- **每个用户各自绑定**：凭据按「平台 + 用户」隔离存储，群聊里互不干扰。
- **无需在服务器上装浏览器**：使用 OAuth 2.0 设备码流程（Device Code Flow），
  在手机或任意电脑的浏览器上完成一次授权即可，不需要公网回调地址、不需要内网穿透。
- **自动续期**：refresh_token 自动滚动刷新（官方默认 90 天），
  失效时给出明确提示而不是静默失败。
- **工具调用 + Skill 双通道**：模型支持函数调用时走 Tool；同时提供 `/todo` 指令兜底
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

在聊天中：

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

绑定后也可以直接对机器人说「帮我看看待办同步状态」，模型会自动调用
`ms_todo_account_status` 等工具。若模型不支持函数调用，用上面的指令即可。

## 指令与工具对照

| 指令 | LLM 工具 | 说明 |
| --- | --- | --- |
| `/todo login` | `ms_todo_login_start` | 发起设备码授权 |
| `/todo status` | `ms_todo_account_status` | 查询绑定状态 |
| `/todo logout` | `ms_todo_logout` | 解绑 |
| `/todo lists` | `ms_todo_list_task_lists` | 列出待办列表 |

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

## 隐私与安全

- 只申请 `Tasks.ReadWrite` / `User.Read` 委派权限，**只能访问授权账号自己的待办**。
- 凭据保存在 AstrBot 的插件级 KV 存储中，按 `作者/插件名` 命名空间隔离；
  日志中不会输出 token 内容。
- `/todo logout` 会立即删除本地凭据。如果怀疑泄露，请同时到
  [Microsoft 账户 → 应用权限](https://account.live.com/consent/Manage) 撤销授权。

## 开发

```bash
# 目录结构
main.py            插件入口：指令 + 工具注册 + 业务逻辑
graph/auth.py      OAuth 设备码授权 / token 续期
graph/client.py    Graph REST 封装（重试、限流、错误分类）
graph/store.py     基于插件 KV 的凭据存储
tools/             FunctionTool 定义（LLM 函数调用）
skills/            插件内置 Skill（M3 加入）
scripts/           开发计划 Excel 生成脚本
```

- 遵循 AstrBot 插件开发规范：使用 `httpx` 异步请求、持久化数据放 `data` 目录
  （本插件使用插件级 KV）、提交前用 `ruff` 格式化。
- 灵感来源：Anthropic Agent Skills 规范、AstrBot 官方插件模板。

## 许可证

AGPL-3.0（继承自 AstrBot 插件模板）。
