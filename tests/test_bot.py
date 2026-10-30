import io
import unittest

from PIL import Image

from bot_image import render_match_png
from discord_bot import search_text


class BotTests(unittest.TestCase):
    def test_match_image_contains_both_teams(self):
        match = {
            "sid": "01123a9d000c36dd", "start_utc": "2026-10-30 10:00 UTC",
            "map": "Moscow", "battle_group": "aircraft", "game_mode": "realistic",
            "players": [
                {"uid": "101", "name": "甲", "team": 1, "score": 1200, "kills": 2,
                 "groundKills": 0, "deaths": 1, "assists": 1,
                 "configured": [{"name": "F-16"}], "max_br": 13.7},
                {"uid": "202", "name": "乙", "team": 2, "score": 900, "kills": 1,
                 "groundKills": 0, "deaths": 1, "assists": 0,
                 "configured": [{"name": "MiG-29"}], "max_br": 13.7},
            ],
            "match_br": 13.7,
        }
        image = Image.open(io.BytesIO(render_match_png(match, "101")))
        self.assertEqual(image.format, "PNG")
        self.assertEqual(image.width, 1500)
        self.assertGreater(image.height, 300)

    def test_search_reply_fits_discord_limit(self):
        items = [{"sid": f"{i:016x}", "date_text": "2026-10-30 10:00 UTC",
                  "map": "x" * 1000, "vehicles": "aircraft", "game_mode": "realistic"}
                 for i in range(15)]
        self.assertLessEqual(len(search_text("player", {"items": items, "total": 500})), 2000)

if __name__ == "__main__":
    unittest.main()
