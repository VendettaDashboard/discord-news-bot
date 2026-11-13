import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from news_core import NewsError, atomic_json_write, load_news, normalize_payload, payload_digest, read_state


class NewsCoreTests(unittest.TestCase):
    def test_accepts_array_and_current_date(self):
        news = normalize_payload(
            [{"headline": "Test", "summary": "Inhalt", "source": "Quelle"}],
            default_date=date(2026, 11, 13),
        )
        self.assertEqual(news["date"], "2026-11-13")
        self.assertEqual(len(news["items"]), 1)

    def test_rejects_empty_payload(self):
        with self.assertRaises(NewsError):
            normalize_payload({"date": "2026-11-13", "items": []})

    def test_rejects_stale_news(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "news.json"
            atomic_json_write(file, {
                "date": "2026-11-12", "items": [{"headline": "X", "summary": "Y"}]
            })
            with self.assertRaises(NewsError):
                load_news(file, expected_date=date(2026, 11, 13))

    def test_rejects_non_http_urls(self):
        news = normalize_payload({
            "date": "2026-11-13",
            "items": [{"headline": "Test", "summary": "Inhalt", "url": "javascript:alert(1)"}],
        })
        self.assertIsNone(news["items"][0]["url"])

    def test_caps_items(self):
        news = normalize_payload({
            "date": "2026-11-13",
            "items": [{"headline": f"Test {i}", "summary": "Inhalt"} for i in range(40)],
        })
        self.assertEqual(len(news["items"]), 20)

    def test_atomic_write_and_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            atomic_json_write(path, {"last_sent_date": "2026-11-13"})
            self.assertEqual(read_state(path)["last_sent_date"], "2026-11-13")
            self.assertEqual(len(payload_digest({"a": 1})), 64)


if __name__ == "__main__":
    unittest.main()
