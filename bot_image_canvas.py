"""Render Discord battle images with WTStat's bundled canvas renderer."""
from __future__ import annotations

import base64
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parent
RENDERER = ROOT / "battle_image_renderer.js"
FLAGS = ROOT / "image_assets" / "flags"
SYMBOL_FONT = ROOT / "image_assets" / "fonts" / "symbols_skyquake.bfc9728c6a6bc73f209a.ttf"
LOCAL_ORIGIN = "https://wtstat-bot.invalid"


def _highlight_uid(data: dict, highlight: str | None) -> str | None:
    if not highlight:
        return None
    wanted = str(highlight).casefold()
    for player in data.get("players") or []:
        uid = player.get("uid")
        name = player.get("name")
        if uid is not None and (str(uid) == wanted or str(name or "").casefold() == wanted):
            return str(uid)
    return None


def _serve_asset(route) -> None:
    path = urlsplit(route.request.url).path
    if path == "/":
        route.fulfill(status=200, content_type="text/html; charset=utf-8",
                      body=("<!doctype html><html><head><meta charset='utf-8'>"
                            "<style>@font-face{font-family:WTSymbols;"
                            "src:url('/fonts/wt-symbols.ttf') format('truetype')}</style>"
                            "</head><body></body></html>"))
        return
    if path == "/fonts/wt-symbols.ttf" and SYMBOL_FONT.is_file():
        route.fulfill(status=200, content_type="font/ttf", path=str(SYMBOL_FONT))
        return
    if path.startswith("/flags/") and path.endswith(".svg"):
        code = path.removeprefix("/flags/").removesuffix(".svg")
        if code in {"us", "de", "su", "gb", "jp", "it", "fr", "se", "cn", "il"}:
            flag = FLAGS / f"{code}.svg"
            if flag.is_file():
                route.fulfill(status=200, content_type="image/svg+xml", path=str(flag))
                return
    route.fulfill(status=404, body="Not found")


def render_match_png(data: dict, highlight: str | None = None) -> bytes:
    """Return the same PNG that WTStat's Save Battle Image button generates."""
    if not RENDERER.is_file():
        raise FileNotFoundError(f"缺少图片渲染器：{RENDERER.name}")
    if not SYMBOL_FONT.is_file():
        raise FileNotFoundError(f"缺少载具标志字体：{SYMBOL_FONT.name}")
    uid = _highlight_uid(data, highlight)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        try:
            context = browser.new_context(viewport={"width": 2400, "height": 900}, device_scale_factor=1)
            try:
                context.route(f"{LOCAL_ORIGIN}/**", _serve_asset)
                page = context.new_page()
                page.goto(f"{LOCAL_ORIGIN}/", wait_until="domcontentloaded")
                page.add_script_tag(path=str(RENDERER))
                url = page.evaluate(
                    """async ({data, uid}) => {
                        const canvas = await window.WTStatBattleImage.render(data, uid);
                        return canvas.toDataURL('image/png');
                    }""",
                    {"data": data, "uid": uid},
                )
                prefix = "data:image/png;base64,"
                if not isinstance(url, str) or not url.startswith(prefix):
                    raise ValueError("浏览器未生成 PNG 图片。")
                return base64.b64decode(url[len(prefix):], validate=True)
            finally:
                context.close()
        finally:
            browser.close()
