import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import bot
from calendar_core import BERLIN, COUNTRIES
from news_core import atomic_json_write, read_state


class Clock(datetime):
    current = datetime(2026, 11, 13, 12, tzinfo=BERLIN)
    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz else cls.current


def calendar(week, now):
    return dict(week_start=week, verified_at=now.isoformat(), coverage_complete=True,
                checked_currencies=list(COUNTRIES), events=[],
                crosscheck={"status":"matched", "sources":["Investing.com", "Forex Factory"]})


class PublishingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        Clock.current = datetime(2026, 11, 13, 12, tzinfo=BERLIN)
        self.channel = Mock(spec=discord.TextChannel)
        self.channel.guild = SimpleNamespace(id=bot.GUILD_ID)
        self.channel.send = AsyncMock(side_effect=self.deliver)
        self.partials = {}
        self.channel.get_partial_message.side_effect = self.partial
        self.counter = 100
        self.client = SimpleNamespace(get_channel=lambda channel_id: self.channel)
        self.patches = [
            patch.object(bot, "datetime", Clock), patch.object(bot, "client", self.client),
            patch.object(bot, "LOCAL_SCHEDULE_ENABLED", True),
            patch.object(bot, "NEWS_FILE", self.root / "news.json"),
            patch.object(bot, "PREVIEW_FILE", self.root / "preview.json"),
            patch.object(bot, "STATE_FILE", self.root / "state.json"),
            patch.object(bot, "PREVIEW_STATE_FILE", self.root / "preview_state.json"),
        ]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    async def deliver(self, **kwargs):
        self.counter += 1
        return SimpleNamespace(id=self.counter)

    def partial(self, message_id):
        if message_id not in self.partials:
            self.partials[message_id] = SimpleNamespace(
                edit=AsyncMock(return_value=SimpleNamespace(id=message_id)),
                delete=AsyncMock())
        return self.partials[message_id]

    def legacy_state(self):
        week = datetime(2026, 11, 16)
        messages = {(week + timedelta(days=i)).date().isoformat(): 501 + i for i in range(5)}
        atomic_json_write(bot.PREVIEW_STATE_FILE, {
            "last_sent_week": "2026-11-16", "last_sent_at": Clock.current.isoformat(),
            "messages": messages, "preview": True})
        return messages

    async def test_single_post_preview_does_not_block_monday_and_week_is_not_resent(self):
        atomic_json_write(bot.PREVIEW_FILE, calendar("2026-11-16", Clock.current))
        self.assertIn("1 Wochenkarte", await bot.publish(preview=True))
        self.assertEqual(self.channel.send.await_count, 1)
        embed = self.channel.send.call_args.kwargs["embed"]
        self.assertEqual(len(embed.fields), 5)
        self.assertTrue(all(not field.inline for field in embed.fields))
        self.assertEqual(embed.fields[0].name, "Monday, 16 November")
        self.assertEqual(embed.fields[-1].name, "Friday, 20 November")
        self.assertFalse(bot.STATE_FILE.exists())
        await bot.publish(preview=True)
        self.assertEqual(self.channel.send.await_count, 1)
        Clock.current = datetime(2026, 11, 16, 6, tzinfo=BERLIN)
        atomic_json_write(bot.NEWS_FILE, calendar("2026-11-16", Clock.current))
        self.assertIn("1 Wochenkarte", await bot.publish())
        self.assertEqual(self.channel.send.await_count, 2)
        self.assertEqual(read_state(bot.STATE_FILE)["last_sent_week"], "2026-11-16")
        await bot.publish()
        self.assertEqual(self.channel.send.await_count, 2)

    async def test_failed_post_can_be_retried(self):
        atomic_json_write(bot.PREVIEW_FILE, calendar("2026-11-16", Clock.current))
        self.channel.send.side_effect = OSError("simulierter Verbindungsabbruch")
        with self.assertLogs(bot.logger, level="ERROR"):
            self.assertIn("fehlgeschlagen", await bot.publish(preview=True))
        self.assertFalse(bot.PREVIEW_STATE_FILE.exists())
        self.channel.send = AsyncMock(side_effect=self.deliver)
        self.assertIn("1 Wochenkarte", await bot.publish(preview=True))
        self.assertEqual(self.channel.send.await_count, 1)
        self.assertEqual(read_state(bot.PREVIEW_STATE_FILE)["message_id"], 101)

    async def test_refresh_consolidates_only_saved_legacy_messages(self):
        atomic_json_write(bot.PREVIEW_FILE, calendar("2026-11-16", Clock.current))
        self.legacy_state()
        self.assertIn("aktualisiert", await bot.publish(preview=True, refresh_existing=True))
        self.channel.send.assert_not_awaited()
        self.partial(501).edit.assert_awaited_once()
        self.partial(501).delete.assert_not_awaited()
        for message_id in (502, 503, 504, 505):
            self.partial(message_id).delete.assert_awaited_once()
        state = read_state(bot.PREVIEW_STATE_FILE)
        self.assertEqual(state["message_id"], 501)
        self.assertEqual(state["format_version"], 2)
        self.assertEqual(state["cleanup_message_ids"], [])
        self.assertEqual(set(self.partials), {501, 502, 503, 504, 505})
        self.assertFalse(bot.STATE_FILE.exists())

    async def test_cleanup_retry_does_not_send_new_message(self):
        atomic_json_write(bot.PREVIEW_FILE, calendar("2026-11-16", Clock.current))
        self.legacy_state()
        self.partial(503).delete.side_effect = OSError("simulierter Fehler")
        with self.assertLogs(bot.logger, level="ERROR"):
            self.assertIn("fehlgeschlagen", await bot.publish(preview=True, refresh_existing=True))
        self.assertEqual(read_state(bot.PREVIEW_STATE_FILE)["cleanup_message_ids"], [503, 504, 505])
        self.partial(503).delete.side_effect = None
        self.assertIn("aktualisiert", await bot.publish(preview=True, refresh_existing=True))
        self.channel.send.assert_not_awaited()
        self.partial(502).delete.assert_awaited_once()
        self.assertEqual(read_state(bot.PREVIEW_STATE_FILE)["cleanup_message_ids"], [])

    async def test_stale_production_data_sends_nothing(self):
        Clock.current = datetime(2026, 11, 16, 6, tzinfo=BERLIN)
        atomic_json_write(bot.NEWS_FILE, calendar("2026-11-16", datetime(2026, 11, 13, 12, tzinfo=BERLIN)))
        self.assertIn("Kein Versand", await bot.publish())
        self.channel.send.assert_not_awaited()

    async def test_native_schedule_prepares_before_six_and_publishes_at_six(self):
        async def refresh(news_path, report_path, *, now):
            report = {"week_start": "2026-11-16", "checked_at": now.isoformat(), "status": "matched"}
            atomic_json_write(report_path, report)
            return report
        async def publish(**kwargs):
            atomic_json_write(bot.STATE_FILE, {"last_sent_week": "2026-11-16"})
            return "1 Wochenkarte versendet."
        Clock.current = datetime(2026, 11, 16, 5, 40, tzinfo=BERLIN)
        with patch.object(bot, "SOURCE_REPORT_FILE", self.root / "sources.json"), \
             patch.object(bot, "refresh_calendar", new=AsyncMock(side_effect=refresh)) as refresh_mock, \
             patch.object(bot, "publish", new=AsyncMock(side_effect=publish)) as publish_mock:
            await bot.weekly_check.coro()
            refresh_mock.assert_awaited_once()
            publish_mock.assert_not_awaited()
            Clock.current = datetime(2026, 11, 16, 5, 50, tzinfo=BERLIN)
            await bot.weekly_check.coro()
            refresh_mock.assert_awaited_once()
            Clock.current = datetime(2026, 11, 16, 6, 0, tzinfo=BERLIN)
            await bot.weekly_check.coro()
            publish_mock.assert_awaited_once()
            await bot.weekly_check.coro()
            publish_mock.assert_awaited_once()

    async def test_blocked_sources_are_retried_every_fifteen_minutes_without_posting(self):
        async def refresh(news_path, report_path, *, now):
            report = {"week_start":"2026-11-16", "checked_at":now.isoformat(), "status":"blocked"}
            atomic_json_write(report_path, report)
            return report
        with patch.object(bot, "SOURCE_REPORT_FILE", self.root / "sources.json"), \
             patch.object(bot, "refresh_calendar", new=AsyncMock(side_effect=refresh)) as refresh_mock, \
             patch.object(bot, "publish", new=AsyncMock()) as publish_mock:
            Clock.current = datetime(2026, 11, 16, 6, 0, tzinfo=BERLIN)
            await bot.weekly_check.coro()
            Clock.current = datetime(2026, 11, 16, 6, 1, tzinfo=BERLIN)
            await bot.weekly_check.coro()
            refresh_mock.assert_awaited_once()
            Clock.current = datetime(2026, 11, 16, 6, 15, tzinfo=BERLIN)
            await bot.weekly_check.coro()
            self.assertEqual(refresh_mock.await_count, 2)
            publish_mock.assert_not_awaited()

    async def test_cloud_handoff_disables_local_source_fetch_and_send(self):
        Clock.current = datetime(2026, 11, 16, 6, 0, tzinfo=BERLIN)
        with patch.object(bot, "LOCAL_SCHEDULE_ENABLED", False), \
             patch.object(bot, "refresh_calendar", new=AsyncMock()) as refresh_mock, \
             patch.object(bot, "publish", new=AsyncMock()) as publish_mock:
            await bot.weekly_check.coro()
            refresh_mock.assert_not_awaited()
            publish_mock.assert_not_awaited()
