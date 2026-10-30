# WTStat Discord Bot

在 Windows 笔记本上独立运行的 War Thunder Replay Discord Bot。项目内包含 WTStat 查询代码、Replay 解析器、载具资料和所需翻译文件；运行时不读取另一个 WTStat 目录，也不需要启动 WTStat 网页服务。

## 功能

- `/wtsearch player_id`：按玩家昵称或数字 UID 搜索官网最近 15 场对局，点击编号按钮输出该场双方最终战绩 PNG。
- `/wtmatch session_id`：按十进制或 16 位十六进制 Session ID 直接输出战绩 PNG。搜索按钮 10 分钟后失效，但 Session ID 命令始终可用。
- 图片包含时间、地图、模式、对局权重、两队玩家的得分、击杀、死亡、助攻、载具和 BR；搜索时高亮对应玩家。

首次读取某场时会下载并解析 Replay，之后数据和 Replay 文件缓存在本项目的 `data/`、`cache/` 中。官网的搜索范围和 Replay 保留期限由 Gaijin 决定。

## 首次配置

需要 Windows、Python 3.12+、网络连接，以及 Discord Bot Token。

1. 在 [Discord Developer Portal](https://discord.com/developers/applications) 创建应用和 Bot，复制 Bot Token。将 Bot 安装到服务器时选择 `bot` 和 `applications.commands`，授予“发送消息”和“附加文件”权限。
2. 复制 `discord.local.example.json` 为 `discord.local.json`，填入 Token。建议填写数字 `guild_id`，命令会在该服务器同步。可在 Discord 开启开发者模式后右键服务器复制 ID。留空会同步全局命令，显示可能需要一些时间。
3. 若要按昵称或 UID 搜索，复制 `config.local.example.json` 为 `config.local.json`，填入你本人在 `warthunder.com` 登录后的 `identity_sid` Cookie。Session ID 直查不需要此 Cookie。Cookie 失效时需自行更新。
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
