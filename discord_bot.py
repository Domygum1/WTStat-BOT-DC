"""Local Discord slash-command front end for WTStat."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from io import BytesIO
from pathlib import Path

import discord
from discord import app_commands
from fastapi import HTTPException

import app as wtstat
from bot_image import render_match_png


CONFIG_PATH = Path(__file__).with_name("discord.local.json")
log = logging.getLogger("wtstat.discord")
detail_lock = asyncio.Lock()


def load_config():
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    except (OSError, ValueError) as error:
        raise SystemExit(f"无法读取 {CONFIG_PATH.name}：{error}") from error
    token = os.environ.get("DISCORD_BOT_TOKEN") or config.get("token")
    if not isinstance(token, str) or not token.strip():
        raise SystemExit("缺少 Discord Bot Token。请填写 discord.local.json 或 DISCORD_BOT_TOKEN。")
    guild_id = os.environ.get("DISCORD_GUILD_ID") or config.get("guild_id")
    if guild_id:
        try:
            guild_id = int(guild_id)
        except (TypeError, ValueError) as error:
            raise SystemExit("guild_id 必须是 Discord 服务器的数字 ID。") from error
    return token.strip(), guild_id


def display_error(error):
    if isinstance(error, HTTPException):
        return str(error.detail)
    log.exception("WTStat Bot 请求失败", exc_info=error)
    return "查询失败，请查看 Bot 运行窗口中的错误信息。"


def safe(value, limit: int = 100):
    raw = str(value or "N/A").replace("\n", " ")
    if len(raw) > limit:
        raw = raw[:limit - 1] + "…"
    return discord.utils.escape_markdown(raw).replace("@", "＠")


def search_text(query, result):
    items = result.get("items") or []
    total = result.get("total")
    lines = [f"**{safe(query, 64)} 最近对局**" + (f" · 官网共 {total} 场" if total is not None else "")]
    if not items:
        lines.append("没有找到对局。官网可查询范围和 Replay 保留时长由 Gaijin 决定。")
    for index, item in enumerate(items, 1):
        group = {"aircraft": "空战", "tank": "陆战", "ship": "海战"}.get(item.get("vehicles"), "对局")
        mode = {"arcade": "街机", "realistic": "历史", "simulation": "全真"}.get(item.get("game_mode"), "")
        lines.append(f"**{index:02d}.** {safe(item.get('date_text'), 28)} · {safe(item.get('map'), 45)} · {group} {mode}\n"
                     f"`{item['sid']}`")
    if items:
        lines.append("点击下方编号查看该场完整战绩图片；也可用 `/wtmatch session_id`。首次读取 Replay 可能较慢。")
    return "\n".join(lines)


async def send_match(interaction: discord.Interaction, session: str, highlight: str | None = None):
    await interaction.response.defer(thinking=True)
    try:
        async with detail_lock:
            data = await asyncio.to_thread(wtstat.detail, session)
            png = await asyncio.to_thread(render_match_png, data, highlight)
        filename = f"WTStat_{data['sid']}.png"
        official_url = f"https://warthunder.com/en/tournament/replay/{int(data['sid'], 16)}"
        message = (f"**{safe(data.get('map') or data.get('level'))}** · "
                   f"{safe(data.get('start_utc'))}\n"
                   f"Session `{data['sid']}` · [官网对局页]({official_url})")
        await interaction.followup.send(message, file=discord.File(BytesIO(png), filename=filename),
                                        allowed_mentions=discord.AllowedMentions.none())
    except Exception as error:
        await interaction.followup.send(display_error(error), ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())


class MatchButton(discord.ui.Button):
    def __init__(self, index: int, sid: str, query: str):
        super().__init__(label=str(index), style=discord.ButtonStyle.primary, row=(index - 1) // 5)
        self.sid = sid
        self.query = query

    async def callback(self, interaction: discord.Interaction):
        await send_match(interaction, self.sid, self.query)


class MatchView(discord.ui.View):
    def __init__(self, items: list[dict], query: str):
        super().__init__(timeout=600)
        for index, item in enumerate(items[:15], 1):
            self.add_item(MatchButton(index, item["sid"], query))


class WTStatBot(discord.Client):
    def __init__(self, guild_id: int | None):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.guild_id = guild_id

    async def setup_hook(self):
        wtstat.sync_bundled_wiki()
        if self.guild_id:
            guild = discord.Object(id=self.guild_id)
            self.tree.copy_global_to(guild=guild)
            commands = await self.tree.sync(guild=guild)
            log.info("已向服务器 %s 同步 %s 个命令", self.guild_id, len(commands))
        else:
            commands = await self.tree.sync()
            log.info("已全局同步 %s 个命令；显示可能需要一些时间", len(commands))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token, guild_id = load_config()
    bot = WTStatBot(guild_id)

    @bot.tree.command(name="wtsearch", description="按 War Thunder 玩家昵称或 UID 查看最近 15 场")
    @app_commands.describe(player_id="玩家昵称或数字 UID")
    async def wtsearch(interaction: discord.Interaction, player_id: str):
        query = player_id.strip()
        if not query or len(query) > 64:
            await interaction.response.send_message("请输入不超过 64 个字符的玩家昵称或 UID。", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            result = await asyncio.to_thread(wtstat.search, query, 0, None, None)
            if result.get("mode") == "session":
                await interaction.followup.send("这是 Session ID，请用 `/wtmatch session_id` 查看战绩图片。", ephemeral=True)
                return
            items = result.get("items") or []
            await interaction.followup.send(search_text(query, result),
                                            view=MatchView(items, query) if items else None,
                                            allowed_mentions=discord.AllowedMentions.none())
        except Exception as error:
            await interaction.followup.send(display_error(error), ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())

    @bot.tree.command(name="wtmatch", description="按 Replay Session ID 输出完整战绩图片")
    @app_commands.describe(session_id="对局的十进制或 16 位十六进制 Session ID")
    async def wtmatch(interaction: discord.Interaction, session_id: str):
        await send_match(interaction, session_id.strip())

    bot.run(token, reconnect=True, log_handler=None)


if __name__ == "__main__":
    main()
