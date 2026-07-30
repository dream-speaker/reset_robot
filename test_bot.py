import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import bot


RSS_SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item>
    <title>Codex &amp; ChatGPT Work usage reset lands in the next hour.</title>
    <link>https://nitter.example/thsottiaux/status/2079609157934886975</link>
    <guid>https://nitter.example/thsottiaux/status/2079609157934886975</guid>
    <pubDate>Wed, 29 Jul 2026 12:00:00 GMT</pubDate>
  </item>
  <item>
    <title>Ordinary product update.</title>
    <link>https://nitter.example/thsottiaux/status/2079609157934886976</link>
    <pubDate>Wed, 29 Jul 2026 12:05:00 GMT</pubDate>
  </item>
</channel></rss>"""


class FeedTests(unittest.TestCase):
    def test_parse_rss(self):
        posts = bot.parse_rss(RSS_SAMPLE)
        self.assertEqual([post.id for post in posts], [
            "2079609157934886975",
            "2079609157934886976",
        ])
        self.assertIn("ChatGPT Work", posts[0].text)
        self.assertEqual(posts[0].created_at.tzinfo, timezone.utc)

    def test_rejects_empty_feed(self):
        with self.assertRaisesRegex(RuntimeError, "no recognizable"):
            bot.parse_rss(b"<rss><channel /></rss>")

    def test_configured_feed_urls(self):
        with patch.dict(
            os.environ,
            {"RSS_FEED_URLS": "https://one/{username}/rss,https://two/u/rss"},
        ):
            self.assertEqual(
                bot.configured_feed_urls(),
                [
                    "https://one/thsottiaux/rss",
                    "https://two/u/rss",
                ],
            )


class ClassificationTests(unittest.TestCase):
    def test_completed_reset(self):
        text = "I've reset usage limits for all ChatGPT Work and Codex users."
        self.assertEqual(bot.classify_reset(text), "completed")

    def test_banked_reset(self):
        text = "Added a banked reset to 500k users of ChatGPT Work and Codex."
        self.assertEqual(bot.classify_reset(text), "completed")

    def test_upcoming_reset(self):
        text = (
            "Tomorrow we will grant the first banked reset across all of our "
            "ChatGPT Work and Codex users."
        )
        self.assertEqual(bot.classify_reset(text), "upcoming")

    def test_next_hour_reset(self):
        text = (
            "New day, new usage reset for paid users of Codex and ChatGPT Work. "
            "Lands in the next hour."
        )
        self.assertEqual(bot.classify_reset(text), "upcoming")

    def test_ignores_unrelated_codex_post(self):
        self.assertIsNone(bot.classify_reset("We shipped a new Codex feature today."))

    def test_ignores_other_product_reset(self):
        self.assertIsNone(bot.classify_reset("I reset my laptop and it works now."))


class StateSelectionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.posts = [
            bot.Post("100", "old", self.now - timedelta(hours=2)),
            bot.Post("101", "recent", self.now - timedelta(minutes=20)),
            bot.Post("102", "newest", self.now - timedelta(minutes=5)),
        ]

    def test_selects_ids_not_seen(self):
        selected = bot.select_new_posts(self.posts, {"100"}, self.now, 60)
        self.assertEqual([post.id for post in selected], ["101", "102"])

    def test_handles_feed_temporarily_returning_older_post(self):
        selected = bot.select_new_posts(self.posts, {"101", "102"}, self.now, 60)
        self.assertEqual([post.id for post in selected], ["100"])

    def test_first_run_uses_lookback(self):
        selected = bot.select_new_posts(self.posts, set(), self.now, 60)
        self.assertEqual([post.id for post in selected], ["101", "102"])

    def test_state_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            with patch.object(bot, "STATE_FILE", state_path):
                bot.write_state({"12345", "12346"})
                self.assertEqual(bot.load_seen_ids(), {"12345", "12346"})

    def test_migrates_original_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state_path.write_text('{"last_seen_id":"12345"}', encoding="utf-8")
            with patch.object(bot, "STATE_FILE", state_path):
                self.assertEqual(bot.load_seen_ids(), {"12345"})


class EmailTests(unittest.TestCase):
    def test_email_contains_link_and_text(self):
        post = bot.Post(
            "2079609157934886975",
            "Codex usage reset lands in the next hour.",
            datetime(2026, 7, 29, tzinfo=timezone.utc),
        )
        with patch.dict(
            os.environ,
            {
                "SMTP_USER": "sender@example.com",
                "EMAIL_TO": "recipient@example.com",
            },
            clear=False,
        ):
            message = bot.build_email([(post, "upcoming")])
        self.assertIn("即将发放", message["Subject"])
        self.assertIn(post.url, message.get_body(preferencelist=("plain",)).get_content())


if __name__ == "__main__":
    unittest.main()
