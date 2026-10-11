# Codex 龙娘界面桥接协议

这是独立本地客户端，使用已安装 Codex 的 `app-server`，不修改官方应用文件。主服务仅监听 `127.0.0.1:8741`，静态文件从项目 `public/` 提供。

## 请求

`POST /api/rpc`，`Content-Type: application/json`：

```json
{"method":"thread/list","params":{"limit":30,"sortKey":"updated_at"}}
```

成功返回 `{"result":...}`；失败返回 `{"error":{"code":-32602,"message":"..."}}`。HTTP 请求时 Origin/Host 必须属于 `http://127.0.0.1:8741` 或 `http://localhost:8741`。不使用跨源请求。

| 方法 | 开放参数 |
| --- | --- |
| account/read | refreshToken（服务始终使用 false，结果只保留账号类型和套餐） |
| account/rateLimits/read | 无 |
| model/list | cursor、limit、includeHidden |
| thread/list | cursor、limit、sortKey、sortDirection、modelProviders、sourceKinds、archived、searchTerm、cwd、projectId、useStateDbOnly |
| thread/read | threadId、includeTurns |
| thread/turns/list | threadId、cursor、limit、sortDirection、itemsView |
| thread/items/list | threadId、turnId、cursor、limit、sortDirection |
| thread/start | cwd、model、projectId |
| thread/resume | threadId、excludeTurns、model |
| thread/archive | threadId |
| turn/start | threadId、input、model、effort |
| turn/interrupt | threadId、turnId |

`limit` 允许 1–100。文字输入：`[{"type":"text","text":"用户消息"}]`。图片输入：`{"type":"image","url":"data:image/png;base64,..."}`，只允许浏览器选取的 PNG/JPEG/WebP 原始 dataURL，每张最多 20 MiB、最多三张；不允许远程 URL 或任意本地路径。这是本客户端的限制，不是 Codex 官方或模型的图片上限。文件输入使用下述上传接口返回的 ID，每次最多十个附件（图片计入总数）。HTTP 请求体总限制 85 MiB，可容纳三张 20 MiB 图片的 base64 数据和正常文字/JSON 开销；超过总量时提示减少附件或分次发送。未指定 model/effort 时遵循 Codex 原生配置；不会覆盖全局模型。界面没有提供权限、沙箱、全局配置、任意文件读取或 shell 接口。

### 文件附件

`POST /api/attachments`，保留同样的本地 Host/Origin 和 JSON 检查：

```json
{"name":"资料.txt","data":"5paH5Lu25YaF5a65"}
```

`data` 是文件原始字节的 base64（不带 dataURL 前缀），单个文件须为 1 字节至 5 MiB。成功返回 `{"result":{"id":"32位随机小写十六进制ID","name":"资料.txt","size":12}}`；名称会处理 Windows 保留名、特殊字符和长度，同时保留常见扩展名。名称只能是文件名，不能包含目录分隔符。返回值不包含本地路径。

发送时，在 `turn/start.input` 中添加 `{"type":"attachment","id":"上传返回的ID"}`。服务检查 ID、持久化元数据和文件一致性，仅将本界面保存的附件转换成单独的原生文字输入项：

```text
[用户附件] {"name":"资料.txt","path":"项目绝对路径/.runtime/attachments/随机ID/file-资料.txt"}
请根据我的消息读取该本地附件。
```

这让 Codex 使用现有文件工具读取文档；不添加全局权限。浏览器传入的任意 `localImage`、`mention`、`skill` 或路径参数仍会被拒绝。未知 ID、文件丢失或大小不符时要求重新添加。上传文件不能通过 HTTP GET 下载，服务不提供读取任意路径的接口。

文件与每个 ID 的元数据保存在本项目 `.runtime/attachments/`，同名文件以独立 ID 隔离。后台重启后仍可识别原 ID，发送失败时可重试。存储上限为 512 个文件和 256 MiB，达到上限后明确报错；不自动删除历史附件。不要将该目录上传 GitHub。符号链接与 Windows 目录联接不能作为附件目录或文件。

读取历史优先用 `thread/read`（`includeTurns: false`）取元数据，再用 `thread/turns/list` 分页读取回合。`itemsView: "full"` 返回回合内已保存的完整内容；默认 `summary` 仅返回显示摘要。两种分页接口都保留原生 `data`、`nextCursor` 和 `backwardsCursor`，继续读取时传入上一页的 `nextCursor`，直到其为 null；显示顺序由 `sortDirection` 决定。需要单独读取某回合的内容时使用 `thread/items/list`，其 `data` 条目包含 `turnId`、`item`。旧版引擎不支持分页时可回退 `thread/read`（`includeTurns: true`）。这些操作只读取已有历史，不发送推理请求，不取得会话写入资格。

自动列表读取使用 `useStateDbOnly: true`，避免每次读取都扫描并修复历史目录；用户手动刷新可传 false 重新同步。用户发送到旧会话前，先 `thread/resume`（`excludeTurns: true`，保留界面已加载的历史），然后 `turn/start`。原生状态为 active 而未返回活动轮次时，先取1轮 summary，以保留草稿并避免启动第二个回复。启动新会话的响应包含 `model`、`reasoningEffort`、`approvalPolicy`、`sandbox`、`thread`；保留原生配置，界面不要硬编码权限字段。

## 状态与流

`GET /api/status` 返回：

```json
{"appId":"codex-dragon-client","version":1,"instanceId":"后台实例的随机标识","connection":"ready","connected":true,"defaultCwd":"仓库所在目录的绝对路径","account":{"account":{"type":"chatgpt","planType":"plus"},"requiresOpenaiAuth":true},"error":null,"pendingRequests":[],"ownedThreadIds":[],"port":8741}
```

`defaultCwd` 由后台从自身安装位置推导，前端仅在用户尚未选择工作目录时使用它；模型与思考深度仍从实时配置读取。

启动器核对 `appId` 后复用现有本地服务，不关闭占用端口的其他应用。Windows 启动器检查父进程的 Job 约束，使用系统允许的独立进程创建方式，并确认新后台不属于父窗口的 Job；无法保证独立运行时明确报错，不静默降级。

`POST /api/reconnect` 使用空 JSON 对象 `{}`，保留同样 Host/Origin/Content-Type 检查，返回 `{"result":状态快照}`。仅在原生引擎已退出时启动新引擎；仍在运行或初始化中的引擎会直接返回当前状态，不重启、不打断回复。原生引擎重建后清除旧会话写入资格和失效确认，前端重新读取历史；该操作不自动恢复会话或发送消息。HTTP 后台本身不在时，需通过桌面启动入口重新打开。

`POST /api/shutdown` 使用空 JSON 对象 `{}`，保留同样检查，成功返回 `{"ok":true}` 后关闭本地服务及其拥有的后台子进程。

`instanceId` 每次创建 HTTP 后台实例时生成，在该实例内稳定；仅重建原生引擎时保持不变。前端发现实例改变时清除旧的 SSE 序号和已恢复会话缓存，再读取当前会话。原生引擎断开时也会清除旧写入资格、审批和停止控制，保留草稿。

`ownedThreadIds` 在状态与连接快照中列出本桥成功新建或恢复、仍持有的会话 ID。读取历史不会取得写入资格；归档、关闭或后台断开会移除资格。前端只对本桥持有的活动回复提供停止操作。

原生 `already has an active writer` 错误转换为 HTTP `409`，保留原生 `error.code`，增加 `error.kind: "thread_in_use"` 并返回中文提示。错误提示不包含底层会话 ID。浏览历史使用 `thread/read`；发送时才按需 `thread/resume`。占用后保留草稿，界面提供显式新会话或重试连接入口，重试连接不自动发送草稿。

`GET /api/events` 使用 SSE。所有数据用 `event: message`，JSON 位于 data。断线重连会重放最近通知并发送连接快照；前端用事件 id 或原生 item id 避免重复。

- 原生通知：`{"type":"notification","method":"item/agentMessage/delta","params":{...}}`
- 连接快照：`{"type":"connection","connection":"ready",...,"pendingRequests":[...]}`
- 待用户确认：`{"type":"server_request","id":7,"method":"item/commandExecution/requestApproval","params":{...}}`
- 已提交：`{"type":"approval_submitted","id":7}`
- 不支持的服务器请求：`{"type":"unsupported_request","method":"..."}`，服务返回明确错误，不自动批准。

## 用户确认

`POST /api/approval`：`{"id":7,"result":{"decision":"accept"}}`。

- 命令和文件审批：`accept`、`acceptForSession`、`decline`、`cancel`；命令有 `availableDecisions` 时只展示其中选项。本界面不提供修改永久执行/网络规则的对象决策。
- 额外权限：`{"permissions":{},"scope":"turn"}` 表示不授予；批准内容须为请求的全部或子集，范围 `turn` 或 `session`。不接受修改审批策略的额外字段。
- 用户问题：`{"answers":{"问题id":{"answers":["用户回答"]}}}`。
- MCP 表单/URL：`{"action":"accept","content":...}` 或 `{"action":"decline","content":null}` / `cancel`。
- 旧版 execCommandApproval/applyPatchApproval 使用 `approved`、`approved_for_session`、`abort` 或 `{"denied":{"rejection":"拒绝原因"}}`；不能发送 v2 的 accept/decline/cancel。本界面拒绝旧版的永久命令、网络和 MCP 策略修改。

必须保留 JSON-RPC `id` 的类型（数字与字符串不能互换）。只可回复仍待处理的请求；原生 `serverRequest/resolved` 会清除它。

## 实现来源

生成 schema 来自本机 Desktop 包 `26.1002.6548.0` 的 Codex `0.162.0-alpha.2`，按 [第三方说明](../../THIRD_PARTY_NOTICES.md) 保留 Apache-2.0 许可证及上游 NOTICE。服务使用选定请求/响应 schema 验证参数，主动拒绝全局配置和权限覆盖字段。初始化由后台完成，前端无需手发 `initialize`。

[官方 App Server 文档](https://learn.chatgpt.com/docs/app-server)

启动（在客户端目录中使用自己的 Python 3.10+，或使用 `launch.cmd`）：

```powershell
python launcher.py --no-window
```

退出只关闭桥接服务自己的 Codex 进程，不影响官方应用或其他会话。程序不读取 auth.json、不记录原生 stderr、不改变 node_repl 配置，也不对现有聊天做批量修改。
