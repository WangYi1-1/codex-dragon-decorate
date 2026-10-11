# Codex 龙娘工坊

连接本机 Codex 的独立聊天界面，带白紫与金色装饰、蕾丝输入框、蝴蝶结、历史侧栏、龙娘背景和动画宠物。它使用已安装 Codex 的 App Server 与现有 ChatGPT 登录，不需要填写 API Key，也不需要保持官方应用窗口打开。

这是一套独立客户端，未修改官方 Codex 安装文件，不是官方主窗口的皮肤补丁。模型与思考深度从当前账号读取；未指定时沿用本机配置。

## 开始使用

需要 Windows、已安装并完成登录的官方 Codex 桌面应用，以及 Python 3.10 或更新版本。当前程序通过 `OpenAI.Codex` 的 Windows 应用包定位内置引擎；仅安装 Codex CLI 不满足这个启动入口的条件。Python 代码仅使用标准库，不需要运行 `pip install`。Edge 用于独立应用窗口；找不到 Edge 时会打开默认浏览器。

1. 克隆仓库，或下载 ZIP 后解压到你自己的项目目录。
2. 进入 `CodexDragonClient`，双击 **launch.cmd**。
3. 首次连接后，在界面设置里填写想让新会话使用的工作目录。默认是仓库所在目录，已有会话保留原来的目录。
4. 需要关闭后台时，双击 **stop.cmd**。

```powershell
git clone https://github.com/WangYi1-1/codex-dragon-decorate.git
cd codex-dragon-decorate\CodexDragonClient
.\launch.cmd
```

启动入口依次查找 `py -3`、PATH 中的 Python，以及当前 Windows 用户已有的 Codex Python 运行时，并确认版本不低于 3.10。找不到时，请安装 [Python](https://www.python.org/downloads/windows/)，启用 Python Launcher 或添加到 PATH。启动脚本的 PowerShell 执行策略参数只作用于本次进程，不改变系统策略。

也可以从终端直接使用已安装的 Python：

```powershell
python launcher.py
python launcher.py --no-window
python launcher.py --stop
```

关闭浏览器窗口不会关闭后台；再次启动会复用已运行的服务。后台只监听 `127.0.0.1:8741`，停止入口只关闭本项目的服务及它自己启动的 Codex 子进程。端口被其他应用占用时会显示错误，不会关闭那个应用。

## 会话与附件

支持创建、继续、搜索和归档本地 Codex 会话，逐步显示回复、命令输出及操作确认，并可停止当前回复。长会话先加载最近 5 轮，使用「加载更早消息」查看之前的内容。自动刷新和名称搜索使用本地索引；「刷新会话」会同步旧历史，可能需要几秒。

图片与文件可以直接拖到输入框；复制截图或浏览器可读取的文件后，按 Ctrl+V 添加。普通文字粘贴不受影响，「＋」也能选择文件。PNG、JPEG、WebP 图片每张最多 20 MiB，每次最多 3 张，按原图发送；其他文件每个最多 5 MiB，一条消息最多 10 个附件。这些是本客户端的限制，实际读取能力取决于模型和文件格式。文件夹请先选出其中的文件。

普通文件副本保存在 `.runtime/attachments/`，会话通过保存位置引用它们。移除草稿附件不会删除已保存的副本。存储最多 512 个文件、合计 256 MiB；清理这些文件会使旧会话中的对应文件引用失效。

同一会话同时只能由一个客户端写入。查看历史不占用写入权；发送时才尝试继续。如果官方 Codex 或另一个窗口仍持有它，工坊会保留草稿和附件，并提示在原窗口继续或另开会话。交接时先结束原窗口的回复，再退出原应用，然后在工坊重试连接；成功后仍需自己按发送，不会自动重复提交。

连接中断时，「重新连接」会尝试恢复引擎并保留当前输入。如果后台服务已经停止，先重新打开 **launch.cmd**。

## 外观与范围

设置支持紫色夜间样式、背景浓淡、背景开关和宠物开关。宠物会根据工作状态切换动作，视线跟随鼠标；鼠标停留约 0.22 秒会挥手，移开后恢复。系统启用减少动态效果时使用静止姿势。独立界面的宠物开关与官方 Codex 的宠物设置互不覆盖。

背景与宠物图册的来源、使用范围见 [素材说明](../docs/ASSETS.md)。

云端聊天、官方内置浏览器、语音、任务调度和插件管理等功能尚未接入，请继续使用官方客户端。本项目不上传或同步会话到额外服务；模型请求仍由 Codex 自己处理。

## 开发与检查

- `launcher.py`：启动、复用、重连与停止本地服务。
- `win_lifecycle.py`：核验 Windows 后台能独立于启动窗口运行；必要时使用固定用途的本地代理，不创建开机自启或计划任务。
- `server.py`：受限 RPC、附件保存和本地 HTTP/SSE 桥接。
- `public/`：界面、装饰、附件输入和宠物交互。
- `protocol/`：开发时使用的 App Server 协议 schema；官方版本更新可能要求重新验证兼容性。

后端与启动器检查只需要 Python；前端检查需要 Node.js，运行界面本身不需要 Node.js：

```powershell
python -m unittest test_bridge.py test_launcher.py
node --test test_frontend.js
```

`.runtime/` 包含本机运行记录、独立浏览器资料和附件，不应提交到 GitHub。历史与登录由本机 Codex 管理，不要上传账号文件或聊天记录。问题反馈可以提供错误描述、系统版本和 Codex 版本；分享截图或日志前先移除私人信息。
