import copy
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from calendar_core import (BERLIN, COUNTRIES, HEADERS, classify, load_calendar, monday,
                           next_monday, normalize_calendar, publication_lock, render_cards, weekly_due)
from news_core import NewsError

NOW = datetime(2026, 11, 13, 12, tzinfo=BERLIN)
WEEK = date(2026, 11, 16)


def event(name="CPI (YoY)", currency="GBP", country="United Kingdom", at="2026-11-18T07:00:00+00:00", **values):
    return dict(source_event=name, currency=currency, country=country, at=at,
                source_url="https://www.investing.com/economic-calendar/", **values)


def payload(events):
    return dict(week_start=WEEK.isoformat(), verified_at=NOW.isoformat(),
                coverage_complete=True, checked_currencies=list(COUNTRIES), events=events)


class CalendarTests(unittest.TestCase):
    def test_only_required_high_groups(self):
        for name in ("Unemployment Rate", "ADP Nonfarm Employment Change", "Average Earnings",
                     "Initial Jobless Claims", "FOMC Meeting Minutes", "Fed Interest Rate Statement",
                     "Interest Rate Press Conference", "Inflation Expectations", "PPI", "PCE", "Holiday",
                     "Private Nonfarm Payrolls", "Government Nonfarm Payrolls"):
            with self.subTest(name=name):
                self.assertIsNone(classify(name, "USD"))
        self.assertEqual(classify("Fed Interest Rate Decision", "USD"), "Interest Rate")
        self.assertEqual(classify("Nonfarm Payrolls", "USD"), "NFP")
        self.assertIsNone(classify("Nonfarm Payrolls", "CAD"))

    def test_euro_zone_only_and_eight_currency_limit(self):
        entries = [
            event(currency="EUR", country="Germany"), event(currency="EUR", country="France"),
            event(currency="EUR", country="Euro Zone"), event(currency="CNY", country="China"),
        ]
        rows = normalize_calendar(payload(entries), WEEK, now=NOW)["events"]
        self.assertEqual([(r["currency"], r["event"]) for r in rows], [("EUR", "CPI")])

    def test_merges_variants_and_prefers_headline_yoy(self):
        entries = [
            event("Trimmed CPI (YoY)", forecast="1.8%", previous="1.9%"),
            event("Median CPI (YoY)", forecast="2.0%", previous="2.1%"),
            event("Core CPI (YoY)", forecast="2.8%", previous="2.6%"),
            event("CPI (MoM)", forecast="-0.4%", previous="0.1%"),
            event("CPI (YoY)", forecast=None, previous="3.5%"),
            event("CPI (YoY)", forecast=None, previous="3.5%"),
        ]
        rows = normalize_calendar(payload(entries), WEEK, now=NOW)["events"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["event"], rows[0]["forecast"], rows[0]["previous"]), ("CPI", "—", "3.5%"))

    def test_japan_converts_to_next_berlin_day(self):
        entry = event("National CPI (YoY)", "JPY", "Japan", "2026-11-19T23:30:00+00:00")
        row = normalize_calendar(payload([entry]), WEEK, now=NOW)["events"][0]
        self.assertEqual((row["date"], row["time"]), ("2026-11-20", "00:30"))

    def test_rejects_unchecked_stale_and_naive_data(self):
        for change in (
            {"coverage_complete": False}, {"checked_currencies": ["USD"]},
            {"verified_at": (NOW - timedelta(hours=25)).isoformat()},
            {"verified_at": "2026-11-13T12:00:00"},
            {"week_start": "2026-11-09"},
        ):
            raw = payload([])
            raw.update(change)
            with self.subTest(change=change), self.assertRaises(NewsError):
                normalize_calendar(raw, WEEK, now=NOW)
        with self.assertRaises(NewsError):
            normalize_calendar(payload([event(at="2026-11-18T07:00:00")]), WEEK, now=NOW)

    def test_no_matching_events_produces_five_no_news_cards(self):
        cards = render_cards(normalize_calendar(payload([]), WEEK, now=NOW))
        self.assertEqual(len(cards), 5)
        self.assertEqual(cards[0]["title"], "Monday, 16 November")
        self.assertEqual(cards[-1]["title"], "Friday, 20 November")
        self.assertTrue(all(card["description"] == "No news" for card in cards))

    def test_columns_values_and_alignment(self):
        entries = [event(forecast="-0.40%", previous="68K"),
                   event("Interest Rate Decision", "CAD", "Canada", "2026-11-19T13:30:00+00:00",
                         forecast="1.25%", previous="3.8%")]
        cards = render_cards(normalize_calendar(payload(entries), WEEK, now=NOW))
        tables = [c["description"].splitlines() for c in cards if c["description"] != "No news"]
        for table in tables:
            self.assertEqual(tuple(table[1].split()), HEADERS)
            self.assertNotIn("|", table[1])
        self.assertEqual(tables[0][1], tables[1][1])
        self.assertIn("-0.40%", cards[2]["description"])
        self.assertIn("68K", cards[2]["description"])

    def test_weekly_timing_catchup_and_duplicate_guard(self):
        self.assertFalse(weekly_due(datetime(2026, 11, 16, 5, 59, tzinfo=BERLIN), {}))
        self.assertTrue(weekly_due(datetime(2026, 11, 16, 6, 0, tzinfo=BERLIN), {}))
        self.assertTrue(weekly_due(datetime(2026, 11, 16, 10, 0, tzinfo=BERLIN), {}))
        self.assertFalse(weekly_due(datetime(2026, 11, 17, 6, 0, tzinfo=BERLIN), {}))
        self.assertFalse(weekly_due(datetime(2026, 11, 16, 6, 0, tzinfo=BERLIN),
                                    {"last_sent_week": "2026-11-16"}))
        self.assertTrue(weekly_due(datetime(2026, 11, 16, 5, 0, tzinfo=timezone.utc), {}))

    def test_summer_time_and_year_boundary(self):
        self.assertTrue(weekly_due(datetime(2027, 3, 29, 4, 0, tzinfo=timezone.utc), {}))
        self.assertFalse(weekly_due(datetime(2027, 3, 29, 3, 59, tzinfo=timezone.utc), {}))
        self.assertEqual(next_monday(date(2026, 12, 31)), date(2027, 1, 4))
        self.assertEqual(monday(date(2027, 1, 1)), date(2026, 12, 28))

    def test_lock_prevents_two_senders_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.lock"
            with publication_lock(path):
                with self.assertRaises(NewsError):
                    with publication_lock(path):
                        self.fail("Zweiter Sender darf nicht eintreten.")
            with publication_lock(path):
                pass
