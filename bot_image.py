"""Render a compact WTStat match card for Discord from /api/detail data."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


WIDTH = 1500
PADDING = 32
GAP = 20
PANEL = (WIDTH - PADDING * 2 - GAP) // 2
BG = "#101821"
CARD = "#1b2836"
ROW = "#243445"
TEXT = "#eef4fa"
MUTED = "#b2c4d3"
BLUE = "#91c7f4"
RED = "#f2a6a5"
GOLD = "#ffd17d"


def _font(size: int, bold: bool = False):
    names = (["msyhbd.ttc", "msyh.ttc"] if bold else ["msyh.ttc", "msyhbd.ttc"])
    roots = [Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu")]
    for root in roots:
        for name in [*names, "DejaVuSans.ttf"]:
            path = root / name
            if path.is_file():
                return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


F_TITLE = _font(30, True)
F_HEAD = _font(22, True)
F_NAME = _font(20, True)
F_BODY = _font(17)
F_SMALL = _font(15)


def _value(value):
    return "N/A" if value is None or value == "" else str(value)


def _rating(value):
    return "N/A" if value is None else f"{float(value):.1f}"


def _fit(draw, value, font, width):
    value = _value(value).replace("\n", " ")
    if draw.textlength(value, font=font) <= width:
        return value
    while value and draw.textlength(value + "…", font=font) > width:
        value = value[:-1]
    return value + "…"


def _sorted_players(players):
    return sorted(players, key=lambda p: (p.get("score") is None, -(p.get("score") or 0)))


def render_match_png(data: dict, highlight: str | None = None) -> bytes:
    """Return a PNG with both teams and their final scores. No network access."""
    players = data.get("players") or []
    teams = {1: [], 2: []}
    for player in players:
        try:
            team = int(player.get("team"))
        except (TypeError, ValueError):
            team = 0
        teams.setdefault(team, []).append(player)
    team_keys = [1, 2, *sorted(key for key in teams if key not in (1, 2))]
    pairs = [team_keys[index:index + 2] for index in range(0, len(team_keys), 2)]
    row_height = 115
    header_height = 55
    meta_height = 142
    heights = [header_height + max((len(teams[key]) for key in pair), default=0) * row_height + 16 for pair in pairs]
    image_height = 118 + meta_height + sum(heights) + GAP * max(0, len(pairs) - 1) + PADDING
    image = Image.new("RGB", (WIDTH, image_height), BG)
    draw = ImageDraw.Draw(image)

    draw.text((PADDING, 26), "WTStat · 对局详情", font=F_TITLE, fill=TEXT)
    draw.text((WIDTH - PADDING, 38), "最终战绩 · 按得分排序", font=F_SMALL, fill=MUTED, anchor="ra")
    draw.rounded_rectangle((PADDING, 82, WIDTH - PADDING, 82 + meta_height), radius=12, fill=CARD)
    group = {"aircraft": "空战", "tank": "陆战", "ship": "海战"}.get(data.get("battle_group"), data.get("battle_group"))
    mode = {"arcade": "街机", "realistic": "历史", "simulation": "全真"}.get(data.get("game_mode"), data.get("game_mode"))
    meta = [
        ("时间 (UTC)", data.get("start_utc")), ("地图", data.get("map") or data.get("level")),
        ("Session ID", data.get("sid")), ("模式", f"{_value(group)} · {_value(mode)}"),
        ("对局权重", _rating(data.get("match_br"))), ("玩家", str(len(players))),
    ]
    col_width = (WIDTH - PADDING * 2 - 60) // 3
    for index, (label, value) in enumerate(meta):
        x = PADDING + 20 + (index % 3) * (col_width + 10)
        y = 94 + (index // 3) * 68
        draw.text((x, y), label, font=F_SMALL, fill=MUTED)
        draw.text((x, y + 23), _fit(draw, value, F_BODY, col_width - 15), font=F_BODY, fill=TEXT)

    top = 118 + meta_height
    for pair, height in zip(pairs, heights):
        for column, team in enumerate(pair):
            x = PADDING + column * (PANEL + GAP)
            accent = BLUE if team == 1 else RED if team == 2 else MUTED
            name = "蓝队" if team == 1 else "红队" if team == 2 else f"队伍 {team}"
            members = _sorted_players(teams[team])
            draw.rounded_rectangle((x, top, x + PANEL, top + height), radius=12, fill=CARD)
            draw.text((x + 18, top + 15), f"{name} · {len(members)} 人", font=F_HEAD, fill=accent)
            ratings = [p.get("max_br") for p in members if p.get("max_br") is not None]
            draw.text((x + PANEL - 18, top + 20), f"最高 BR {_rating(max(ratings) if ratings else None)}",
                      font=F_SMALL, fill=accent, anchor="ra")
            for index, player in enumerate(members):
                y = top + header_height + index * row_height
                selected = highlight and (str(player.get("uid")) == highlight or str(player.get("name", "")).casefold() == highlight.casefold())
                draw.rounded_rectangle((x + 12, y, x + PANEL - 12, y + row_height - 8), radius=8,
                                       fill="#344337" if selected else ROW)
                if selected:
                    draw.rectangle((x + 12, y + 6, x + 17, y + row_height - 14), fill=GOLD)
                draw.text((x + 26, y + 9), _fit(draw, player.get("name"), F_NAME, PANEL - 175),
                          font=F_NAME, fill=GOLD if selected else TEXT)
                draw.text((x + PANEL - 25, y + 14), f"BR {_rating(player.get('max_br'))}",
                          font=F_SMALL, fill=accent, anchor="ra")
                metrics = (f"得分 {_value(player.get('score'))}   对空 {_value(player.get('kills'))}"
                           f"   对地 {_value(player.get('groundKills'))}   死亡 {_value(player.get('deaths'))}"
                           f"   助攻 {_value(player.get('assists'))}")
                draw.text((x + 26, y + 40), _fit(draw, metrics, F_BODY, PANEL - 55), font=F_BODY, fill=TEXT)
                vehicles = " · ".join(_value(v.get("name") or v.get("code")) for v in player.get("configured") or []) or "N/A"
                draw.text((x + 26, y + 69), _fit(draw, "载具 " + vehicles, F_SMALL, PANEL - 55),
                          font=F_SMALL, fill=MUTED)
        top += height + GAP

    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
