# WTStat Discord Bot

在 Windows 笔记本上独立运行的 War Thunder Replay Discord Bot。项目内包含 WTStat 查询代码、Replay 解析器、载具资料和所需翻译文件；运行时不读取另一个 WTStat 目录，也不需要启动 WTStat 网页服务。

## 功能

- `/wtsearch player_name`：按玩家昵称或数字 UID 搜索官网最近 15 场对局，点击编号按钮输出该场双方最终战绩 PNG。
- `/wtmatch session_id`：按十进制或 16 位十六进制 Session ID 直接输出战绩 PNG。搜索按钮 10 分钟后失效，但 Session ID 命令始终可用。
- `/wtwatch player_name duration`：监听本频道的一个玩家；`duration` 可省略，默认 `1h`，也支持 `30m`、`2h`、`30分钟` 等，范围为 1 分钟至 24 小时。Bot 每 15 秒向官网刷新最近对局列表，发现新场次后在本频道发送该局战绩 PNG。启动时已有对局作为基线，不补发旧图。同一频道再次使用会替换原监听；Bot 重启后会恢复尚未到期的监听。
- `/wtunwatch`：提前停止本频道的监听。
- 图片包含时间、地图、模式、对局权重、两队玩家的得分、击杀、死亡、助攻、载具和 BR；搜索时高亮对应玩家。

首次读取某场时通常只下载 Replay 的结算分段并解析，之后数据和 Replay 文件缓存在本项目的 `data/`、`cache/` 中。Bot 内部串行查询，并将自身请求间隔设为至少 1.5 秒。它不启动 WTStat 或 WTStat all，不占用它们的端口，也不读写它们的数据库或缓存。持续监听会每 15 秒向官网发起一次查询；多个程序同时访问 Gaijin 官网时仍可能遇到官网统一的频率限制，不能保证完全不影响其他查询。官网的搜索范围和 Replay 保留期限由 Gaijin 决定。

## 首次配置

需要 Windows、Python 3.12+、网络连接，以及 Discord Bot Token。

1. 在 [Discord Developer Portal](https://discord.com/developers/applications) 创建应用和 Bot，复制 Bot Token。将 Bot 安装到服务器时选择 `bot` 和 `applications.commands`，授予“发送消息”和“附加文件”权限。
2. 复制 `discord.local.example.json` 为 `discord.local.json`，填入 Token。建议填写数字 `guild_id`，命令会在该服务器同步。可在 Discord 开启开发者模式后右键服务器复制 ID。留空会同步全局命令，显示可能需要一些时间。
   如果启动时报 `403 Missing Access`，请核对 `guild_id` 是否为目标**服务器**的 ID，并确认此 Bot 已通过 OAuth2 安装到该服务器，包含 `bot` 和 `applications.commands`。启动脚本会保留错误窗口，便于查看具体信息。
3. 若要按昵称或 UID 搜索，可选择手动 Cookie 或自动登录：
   - 手动：复制 `config.local.example.json` 为 `config.local.json`，填入 `warthunder.com` 登录后的 `identity_sid`。
   - 自动：复制 `login.local.example.json` 为 `login.local.json`，填写专供 Bot 使用的 Gaijin 小号邮箱和密码。Windows 需安装 Microsoft Edge。若目录里已有旧账号的 `config.local.json`，先删除该文件，Bot 才会立即用小号登录。Bot 使用 `work/gaijin_edge_profile/` 作为独立浏览器配置；首次缺少 Cookie 或网站登录失效时才尝试登录，并将新 Cookie 写入本目录的 `config.local.json`。失败后 10 分钟内不会再次尝试，避免连续提交密码。普通登录会在后台进行；如网站要求验证码或两步验证，在本目录运行 `.venv\Scripts\python.exe gaijin_login.py`，在出现的 Edge 窗口里人工完成验证。验证结束后重新使用命令；若监听因认证失败已停止，重新启动 `/wtwatch`。
   Session ID 直查不需要 Cookie。自动登录取决于 Gaijin 当前网页流程，不保证永久无人值守。`login.local.json`、Cookie 和 Edge 配置仅保存在本项目本机，已被 Git 忽略；请勿将账号密码发到 Discord 或提交到 GitHub。
4. 双击 `start_discord_bot.bat`。首次运行会在本目录创建 `.venv` 并安装依赖。窗口保持打开即可持续运行；关闭窗口会停止 Bot。

也可以用环境变量 `DISCORD_BOT_TOKEN` 和 `DISCORD_GUILD_ID` 覆盖 Discord 配置文件。两个本地配置文件均被 Git 忽略，切勿提交或分享 Token、Cookie。

## 登录后自动运行

配置 Token 后，在 PowerShell 中运行：

```powershell
cd C:\Desktop\WTStat_BOT_DC
powershell -ExecutionPolicy Bypass -File .\enable_discord_autostart.ps1
```

这会为当前 Windows 用户创建登录时启动的计划任务，并在异常退出后最多重试 3 次。笔记本必须保持开机、联网且不休眠，Bot 才能持续在线。`discord.py` 会自动重连临时断开的 Discord 连接。

## 项目组成与许可

- `app.py`：随项目内置的 WTStat 搜索、下载、Replay 解析和缓存代码。Bot 直接调用其中的函数，网页路由无需运行。
- `bot_image.py`、`discord_bot.py`：Discord 命令与 PNG 图片生成。
- `bin/wtstat_results.exe` 和 `vendor/wrpl-inspector/`：Replay 解析器及对应源码。
- `data/wiki_vehicles.csv`、`data/missile_names.csv` 和 `vendor/WT-Plotter/resources/translations/`：本地资料。

项目代码及解析器按根目录 `LICENSE` 和 `vendor/wrpl-inspector/LICENSE` 中的 AGPL-3.0 条款提供。WT-Plotter 翻译文件保留其原有许可证，见 `vendor/WT-Plotter/LICENSE` 与 `LICENSE.txt`。
