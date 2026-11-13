import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import calendar_sources as sources
from calendar_core import BERLIN, COUNTRIES, load_calendar, normalize_calendar
from news_core import NewsError, atomic_json_write

NOW = datetime(2026, 11, 13, 12, tzinfo=BERLIN)
WEEK = date(2026, 11, 9)


def ff(title="CPI y/y", currency="USD", at="2026-11-10T08:30:00-05:00", **values):
    return dict(title=title, country=currency, date=at, impact="High",
                forecast=values.get("forecast", ""), previous=values.get("previous", ""))


def payload(name="CPI (YoY)", at="2026-11-10T13:30:00+00:00", forecast="3.9%", previous="3.7%"):
    return sources.base_payload(WEEK, NOW, [
        dict(at=at, currency="USD", country="United States", source_event=name,
             forecast=forecast, previous=previous, source_url="https://www.investing.com/economic-calendar/cpi-733")])


class SourceTests(unittest.TestCase):
    def test_ff_filters_national_eur_and_excluded_events(self):
        rows = [
            ff("German Final CPI m/m", "EUR"), ff("French CPI y/y", "EUR"),
            ff("Final CPI y/y", "EUR"), ff("Unemployment Rate"), ff("ADP Non-Farm Employment Change"),
            ff("Non-Farm Employment Change"), ff("Official Cash Rate", "NZD"),
        ]
        calendar = sources.parse_ff(rows, WEEK, NOW)
        normalized = normalize_calendar(calendar, WEEK, now=NOW)
        self.assertEqual({(r["currency"], r["event"]) for r in normalized["events"]},
                         {("EUR", "CPI"), ("USD", "NFP"), ("NZD", "Interest Rate")})

    def test_ff_preserves_values_and_converts_timezone(self):
        calendar = sources.parse_ff([ff("CPI m/m", forecast="-0.40%", previous="3.8%")], WEEK, NOW)
        row = normalize_calendar(calendar, WEEK, now=NOW)["events"][0]
        self.assertEqual((row["time"], row["forecast"], row["previous"]), ("14:30", "-0.40%", "3.8%"))

    def test_stale_feed_and_missing_timezone_rejected(self):
        with self.assertRaises(sources.SourceError):
            sources.parse_ff([ff(at="2026-11-03T08:30:00-05:00")], WEEK, NOW)
        with self.assertRaises(sources.SourceError):
            sources.parse_ff([ff(at="2026-11-10T08:30:00")], WEEK, NOW)

    def test_matching_sources_are_accepted(self):
        result = sources.compare_sources(payload(), payload(), WEEK, NOW)
        self.assertTrue(result["coverage_complete"])
        self.assertEqual(result["crosscheck"]["status"], "matched")

    def test_value_difference_keeps_primary_values(self):
        result = sources.compare_sources(payload(), payload(forecast="3.8%"), WEEK, NOW)
        self.assertEqual(result["crosscheck"]["status"], "checked_with_differences")
        self.assertEqual(result["events"][0]["forecast"], "3.9%")

    def test_cpi_variants_are_not_compared_as_same_measure(self):
        result = sources.compare_sources(payload(), payload(name="CPI (MoM)", forecast="0.3%"), WEEK, NOW)
        self.assertEqual([item["kind"] for item in result["crosscheck"]["differences"]], ["variant"])

    def test_time_and_coverage_conflicts_need_review(self):
        result = sources.compare_sources(payload(), payload(at="2026-11-11T13:30:00+00:00"), WEEK, NOW)
        self.assertFalse(result["coverage_complete"])
        self.assertEqual(result["crosscheck"]["status"], "review_required")
        result = sources.compare_sources(payload(), sources.base_payload(WEEK, NOW, []), WEEK, NOW)
        self.assertFalse(result["coverage_complete"])

    def test_production_requires_both_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "news.json"
            atomic_json_write(path, payload())
            with self.assertRaises(NewsError):
                load_calendar(path, WEEK, now=NOW, require_crosscheck=True)
            checked = sources.compare_sources(payload(), payload(), WEEK, NOW)
            atomic_json_write(path, checked)
            self.assertEqual(len(load_calendar(path, WEEK, now=NOW, require_crosscheck=True)["events"]), 1)

    def test_investing_table_country_time_and_values(self):
        html = """<table>
        <tr id="eventRowId_42" data-event-datetime="2026/11/10 13:30:00">
        <td class="first left">13:30</td>
        <td class="flagCur"><span title="United States"></span>USD</td>
        <td class="left event"><a href="/economic-calendar/cpi-733">CPI (YoY)</a></td>
        <td id="eventForecast_42">-0.4%</td><td id="eventPrevious_42">3.8%</td></tr>
        <tr id="eventRowId_43" data-event-datetime="2026/11/10 07:00:00">
        <td class="first left">07:00</td>
        <td class="flagCur"><span title="Germany"></span>EUR</td>
        <td class="left event">German CPI (YoY)</td>
        <td id="eventForecast_43">2.0%</td><td id="eventPrevious_43">2.1%</td></tr>
        </table>"""
        events, ids = sources.parse_investing_html(html)
        self.assertEqual(ids, ["eventRowId_42", "eventRowId_43"])
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["forecast"], events[0]["previous"]), ("-0.4%", "3.8%"))
        self.assertEqual(events[0]["at"], "2026-11-10T13:30:00+00:00")


class FetchingTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunked_response_is_read_completely(self):
        async def chunks(size):
            for part in (b'[{"a":', b'1}', b']'):
                yield part
        response = SimpleNamespace(status=200, content=SimpleNamespace(iter_chunked=chunks))
        self.assertEqual(await sources.response_json(response, "Test"), [{"a": 1}])

    async def test_source_failure_keeps_existing_calendar(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "news.json"
            report_path = Path(directory) / "report.json"
            path.write_text('{"keep":"existing"}', encoding="utf-8")
            with patch.object(sources, "fetch_investing", new=AsyncMock(side_effect=sources.SourceError("Investing.com: HTTP 403"))), \
                 patch.object(sources, "fetch_ff", new=AsyncMock(return_value=payload())):
                report = await sources.refresh_calendar(path, report_path, now=NOW)
            self.assertEqual(report["status"], "blocked")
            self.assertTrue(report["sources"]["Forex Factory"]["ok"])
            self.assertEqual(path.read_text(encoding="utf-8"), '{"keep":"existing"}')
            self.assertTrue(report_path.exists())
