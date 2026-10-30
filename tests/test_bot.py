import io
import json
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from PIL import Image

import app
import discord_bot
import gaijin_login
from bot_image_canvas import render_match_png
from bot_watch import WatchStore, merge_scan, parse_duration
from discord_bot import image_data, search_text


class BotTests(unittest.TestCase):
    @staticmethod
    def temp_directory():
        work = Path(__file__).resolve().parents[1] / "work"
        work.mkdir(exist_ok=True)
        return tempfile.TemporaryDirectory(dir=work)

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
        self.assertEqual(image.width, 2400)
        self.assertGreater(image.height, 300)

    def test_search_reply_fits_discord_limit(self):
        items = [{"sid": f"{i:016x}", "date_text": "2026-10-30 10:00 UTC",
                  "map": "x" * 1000, "vehicles": "aircraft", "game_mode": "realistic"}
                 for i in range(15)]
        self.assertLessEqual(len(search_text("player", {"items": items, "total": 500})), 2000)

    def test_statistics_summary_maps_to_image(self):
        data = image_data({"sid": "01123a9d000c36dd", "players": [
            {"uid": "101", "name": "甲", "team": 1, "max_br": 13.7,
             "configured_vehicles": [{"name": "F-16"}],
             "stats": {"score": 1200, "kills": 2}}]})
        self.assertEqual(data["players"][0]["configured"][0]["name"], "F-16")
        self.assertEqual(data["players"][0]["score"], 1200)

    def test_watch_duration_and_new_match_queue_survive_restart(self):
        self.assertEqual(parse_duration("1h"), 3600)
        self.assertEqual(parse_duration("30分钟"), 1800)
        with self.assertRaises(ValueError):
            parse_duration("25h")
        with self.temp_directory() as directory:
            path = Path(directory) / "watch.sqlite3"
            store = WatchStore(path)
            job = store.replace(123, "PlayerName", 3600, ["old"])
            updated = merge_scan(job, ["newest", "newer", "old"])
            self.assertEqual(updated.pending, ["newer", "newest"])
            self.assertTrue(store.save(updated))
            reopened = WatchStore(path).get(123)
            self.assertEqual(reopened.pending, ["newer", "newest"])
            self.assertEqual(merge_scan(reopened, ["newest", "newer", "old"]).pending,
                             ["newer", "newest"])
            replacement = store.replace(123, "Another", 3600, [])
            self.assertFalse(store.save(updated))
            self.assertEqual(store.get(123).id, replacement.id)

    def test_force_refresh_bypasses_five_minute_search_cache(self):
        raw = {"items": [{"sessionId": 8278619900504942392, "startTime": 1700000000,
                           "missionName": "Moscow", "gameMode": "realistic",
                           "statisticGroup": "aircraft"}], "total_count": 1}
        with self.temp_directory() as directory:
            with mock.patch.object(app, "DB", Path(directory) / "test.sqlite3"), \
                 mock.patch.object(app, "replay_api_page", return_value=raw) as official:
                with closing(app.db()) as connection:
                    connection.execute("REPLACE INTO pages VALUES (?,?,?,?)",
                                       ("api:USERNAME:PlayerName", 1, time.time(),
                                        json.dumps({"items": [{"sid": "old"}], "total": 1})))
                    connection.commit()
                self.assertEqual(app.site_page("PlayerName", 1)["items"][0]["sid"], "old")
                official.assert_not_called()
                fresh = app.site_page("PlayerName", 1, force_refresh=True)
                self.assertEqual(fresh["items"][0]["map"], "Moscow")
                official.assert_called_once()

    def test_expired_cookie_triggers_one_login_and_retries_search(self):
        expired = mock.Mock(status_code=200, headers={"Content-Type": "text/html"})
        valid = mock.Mock(status_code=200, headers={"Content-Type": "application/json"})
        valid.json.return_value = {"items": [], "total_count": 0}
        with mock.patch.object(app, "request", side_effect=[expired, valid]) as official, \
             mock.patch.object(app, "refresh_site_login", return_value="new") as login, \
             mock.patch.object(gaijin_login, "configured", return_value=True):
            self.assertEqual(app.replay_api_page({})["total_count"], 0)
        self.assertEqual(official.call_count, 2)
        login.assert_called_once_with()
        expired.close.assert_called_once()
        valid.close.assert_called_once()

    def test_missing_cookie_uses_auto_login_before_request(self):
        response = mock.Mock(status_code=200)
        with mock.patch.object(app, "cookie", return_value=None), \
             mock.patch.object(app, "refresh_site_login", return_value="new") as login, \
             mock.patch.object(app.requests, "request", return_value=response) as official, \
             mock.patch.object(app.time, "sleep"):
            self.assertIs(app.request("https://warthunder.com/en/api/replay", auth=True), response)
        login.assert_called_once_with()
        self.assertEqual(official.call_args.kwargs["cookies"], {"identity_sid": "new"})

    def test_watch_commands_register(self):
        with self.temp_directory() as directory, \
             mock.patch.object(app, "DB", Path(directory) / "commands.sqlite3"), \
             mock.patch.object(discord_bot, "load_config", return_value=("unused", None)), \
             mock.patch.object(discord_bot.WTStatBot, "run", autospec=True) as run:
            discord_bot.main()
            bot = run.call_args.args[0]
            self.assertEqual({command.name for command in bot.tree.get_commands()},
                             {"wtsearch", "wtmatch", "wtwatch", "wtunwatch"})

if __name__ == "__main__":
    unittest.main()
