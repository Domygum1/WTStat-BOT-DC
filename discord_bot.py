"""Local Discord slash-command front end for WTStat."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import discord
from discord import app_commands
from fastapi import HTTPException

import app as wtstat
from bot_image_canvas import render_match_png
from bot_watch import WATCH_INTERVAL, WatchJob, WatchStore, merge_scan, parse_duration


CONFIG_PATH = Path(__file__).with_name("discord.local.json")
log = logging.getLogger("wtstat.discord")
request_lock = asyncio.Lock()


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


def fresh_player_page(player: str) -> dict:
    """Fetch the official first page, bypassing WTStat's five-minute search cache."""
    kind = "ID" if player.isdecimal() else "USERNAME"
    return wtstat.site_page(player, 1, kind, force_refresh=True)


def match_caption(data: dict) -> str:
    official_url = f"https://warthunder.com/en/tournament/replay/{int(data['sid'], 16)}"
    return (f"**{safe(data.get('map') or data.get('level'))}** · "
            f"{safe(data.get('start_utc'))}\n"
            f"Session `{data['sid']}` · [官网对局页]({official_url})")


async def send_match(interaction: discord.Interaction, session: str, highlight: str | None = None):
    await interaction.response.defer(thinking=True)
    try:
        async with request_lock:
            summary = await asyncio.to_thread(wtstat.match_statistics, session)
            data = image_data(summary)
            png = await asyncio.to_thread(render_match_png, data, highlight)
        filename = f"WTStat_{data['sid']}.png"
        await interaction.followup.send(match_caption(data), file=discord.File(BytesIO(png), filename=filename),
                                        allowed_mentions=discord.AllowedMentions.none())
    except Exception as error:
        await interaction.followup.send(display_error(error), ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())


def image_data(summary: dict) -> dict:
    """Map WTStat's low-download result summary to the PNG renderer's fields."""
    return {
        **{key: summary.get(key) for key in
           ("sid", "start_utc", "map", "battle_group", "game_mode", "match_br", "source", "parts")},
        "players": [
            {"uid": player.get("uid"), "name": player.get("name"),
             "team": player.get("team"), "max_br": player.get("max_br"),
             "configured": player.get("configured_vehicles") or [],
             **(player.get("stats") or {})}
            for player in summary.get("players") or []
        ],
    }


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
        self.watch_store = WatchStore(wtstat.DB)
        self.watch_tasks: dict[int, asyncio.Task] = {}
        self.watch_errors: dict[int, int] = {}

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

    async def on_ready(self):
        for job in self.watch_store.list_active():
            if job.channel_id not in self.watch_tasks or self.watch_tasks[job.channel_id].done():
                self.launch_watch(job, immediate=True)
        log.info("Discord 已连接：%s；运行中的监听：%s", self.user, len(self.watch_tasks))

    def launch_watch(self, job: WatchJob, *, immediate: bool = False):
        previous = self.watch_tasks.get(job.channel_id)
        if previous and not previous.done():
            previous.cancel()
        task = asyncio.create_task(self.watch_loop(job.channel_id, job.id, immediate=immediate),
                                   name=f"wtwatch:{job.channel_id}")
        self.watch_tasks[job.channel_id] = task

        def finished(done):
            if self.watch_tasks.get(job.channel_id) is done:
                self.watch_tasks.pop(job.channel_id, None)
            if not done.cancelled():
                error = done.exception()
                if error:
                    log.error("监听任务意外退出：频道 %s", job.channel_id,
                              exc_info=(type(error), error, error.__traceback__))

        task.add_done_callback(finished)

    def stop_watch(self, channel_id: int) -> bool:
        removed = self.watch_store.delete(channel_id)
        task = self.watch_tasks.pop(channel_id, None)
        if task and not task.done():
            task.cancel()
        self.watch_errors.pop(channel_id, None)
        return removed

    async def watch_loop(self, channel_id: int, watch_id: str, *, immediate: bool):
        next_check = time.monotonic() if immediate else time.monotonic() + WATCH_INTERVAL
        while True:
            job = self.watch_store.get(channel_id)
            if not job or job.id != watch_id:
                return
            if time.time() >= job.expires_at:
                self.watch_store.delete(channel_id, watch_id)
                try:
                    await self.get_partial_messageable(channel_id).send(
                        f"`/wtwatch` 对 **{safe(job.player, 64)}** 的监听已结束。",
                        allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    pass
                return
            await asyncio.sleep(max(0, min(next_check - time.monotonic(), job.expires_at - time.time())))
            next_check = max(next_check + WATCH_INTERVAL, time.monotonic() + WATCH_INTERVAL)
            job = self.watch_store.get(channel_id)
            if not job or job.id != watch_id or time.time() >= job.expires_at:
                continue
            try:
                await self.poll_watch(job)
                self.watch_errors.pop(channel_id, None)
            except asyncio.CancelledError:
                raise
            except HTTPException as error:
                log.warning("监听 %s 查询失败：%s", channel_id, error.detail)
                if error.status_code in (401, 403):
                    self.watch_store.delete(channel_id, watch_id)
                    await self.watch_notice(channel_id, f"监听已停止：{safe(error.detail, 300)}")
                    return
                count = self.watch_errors.get(channel_id, 0) + 1
                self.watch_errors[channel_id] = count
                if count == 3:
                    await self.watch_notice(channel_id, "监听连续查询失败，Bot 会继续每 15 秒重试；请查看运行窗口。")
            except Exception:
                log.exception("监听 %s 出错", channel_id)
                count = self.watch_errors.get(channel_id, 0) + 1
                self.watch_errors[channel_id] = count
                if count == 3:
                    await self.watch_notice(channel_id, "监听连续查询失败，Bot 会继续每 15 秒重试；请查看运行窗口。")

    async def watch_notice(self, channel_id: int, message: str):
        try:
            await self.get_partial_messageable(channel_id).send(
                message, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning("无法向频道 %s 发送监听提示", channel_id)

    async def poll_watch(self, job: WatchJob):
        async with request_lock:
            page = await asyncio.to_thread(fresh_player_page, job.player)
        current = [item["sid"] for item in page.get("items") or []]
        updated = merge_scan(job, current)
        if not self.watch_store.save(updated):
            return
        for sid in list(updated.pending):
            latest = self.watch_store.get(job.channel_id)
            if not latest or latest.id != job.id or time.time() >= latest.expires_at:
                return
            try:
                async with request_lock:
                    summary = await asyncio.to_thread(wtstat.match_statistics, sid)
                    data = image_data(summary)
                    png = await asyncio.to_thread(render_match_png, data, job.player)
            except (HTTPException, OSError, ValueError) as error:
                log.warning("新对局 %s 尚无法生成图片，稍后重试：%s", sid, error)
                continue
            latest = self.watch_store.get(job.channel_id)
            if not latest or latest.id != job.id or time.time() >= latest.expires_at:
                return
            try:
                await self.get_partial_messageable(job.channel_id).send(
                    f"**{safe(job.player, 64)} 新对局**\n{match_caption(data)}",
                    file=discord.File(BytesIO(png), filename=f"WTStat_{sid}.png"),
                    allowed_mentions=discord.AllowedMentions.none())
            except (discord.Forbidden, discord.NotFound):
                log.warning("无法向频道 %s 发图，监听已停止", job.channel_id)
                self.watch_store.delete(job.channel_id, job.id)
                return
            except discord.HTTPException as error:
                log.warning("向频道 %s 发图失败，稍后重试：%s", job.channel_id, error)
                continue
            updated = replace(latest, pending=[item for item in latest.pending if item != sid])
            if not self.watch_store.save(updated):
                return


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token, guild_id = load_config()
    bot = WTStatBot(guild_id)

    @bot.tree.command(name="wtsearch", description="按 War Thunder 玩家昵称或 UID 查看最近 15 场")
    @app_commands.describe(player_name="玩家昵称或数字 UID")
    async def wtsearch(interaction: discord.Interaction, player_name: str):
        query = player_name.strip()
        if not query or len(query) > 64:
            await interaction.response.send_message("请输入不超过 64 个字符的玩家昵称或 UID。", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            async with request_lock:
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

    @bot.tree.command(name="wtmatch", description="按 Replay Session ID 输出战绩图片")
    @app_commands.describe(session_id="对局的十进制或 16 位十六进制 Session ID")
    async def wtmatch(interaction: discord.Interaction, session_id: str):
        await send_match(interaction, session_id.strip())

    @bot.tree.command(name="wtwatch", description="每 15 秒检查玩家新对局并在本频道发送战绩图片")
    @app_commands.describe(player_name="要监听的玩家昵称或数字 UID",
                           duration="监听时间，例如 30m、1h、2h；默认 1h")
    async def wtwatch(interaction: discord.Interaction, player_name: str, duration: str = "1h"):
        player = player_name.strip()
        if not player or len(player) > 64 or not interaction.channel_id:
            await interaction.response.send_message("请输入不超过 64 个字符的玩家昵称，并在频道中使用命令。", ephemeral=True)
            return
        try:
            seconds = parse_duration(duration)
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            async with request_lock:
                page = await asyncio.to_thread(fresh_player_page, player)
            baseline = [item["sid"] for item in page.get("items") or []]
            job = bot.watch_store.replace(interaction.channel_id, player, seconds, baseline)
            bot.launch_watch(job)
            await interaction.followup.send(
                f"已在本频道监听 **{safe(player, 64)}**。每 {WATCH_INTERVAL} 秒检查一次，"
                f"到 <t:{int(job.expires_at)}:F> 自动结束。现有 {len(baseline)} 场作为基线，不会补发旧对局。"
                "使用 `/wtunwatch` 可提前停止。",
                allowed_mentions=discord.AllowedMentions.none())
        except Exception as error:
            await interaction.followup.send(display_error(error), ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())

    @bot.tree.command(name="wtunwatch", description="停止本频道的玩家新对局监听")
    async def wtunwatch(interaction: discord.Interaction):
        if not interaction.channel_id or not bot.stop_watch(interaction.channel_id):
            await interaction.response.send_message("本频道没有正在运行的监听。", ephemeral=True)
            return
        await interaction.response.send_message("已停止本频道的玩家监听。")

    try:
        bot.run(token, reconnect=True, log_handler=None)
    except discord.Forbidden as error:
        if guild_id and error.code == 50001:
            raise SystemExit(
                f"Bot 无法访问 Discord 服务器 {guild_id}。请确认这是服务器 ID，"
                "并将 Bot 安装到该服务器，授权 bot 和 applications.commands。"
            ) from None
        raise


if __name__ == "__main__":
    main()
