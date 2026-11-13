import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import cloud_run
from calendar_core import BERLIN, COUNTRIES
from cloud_state import StateError
from news_core import atomic_json_write

MONDAY = datetime(2026,11,16,6,0,tzinfo=BERLIN)
FRIDAY = datetime(2026,11,13,12,0,tzinfo=BERLIN)


def payload(now,week="2026-11-16"):
    return {"week_start":week,"verified_at":now.isoformat(),"coverage_complete":True,
            "checked_currencies":list(COUNTRIES),"events":[],
            "crosscheck":{"status":"matched","sources":["Investing.com","Forex Factory"]}}


class Store:
    def __init__(self,state=None,fail_save=None):
        self.value=copy.deepcopy(state or {})
        self.saved=[]
        self.loads=0
        self.fail_save=fail_save
    async def load(self):
        self.loads+=1
        return copy.deepcopy(self.value)
    async def save(self,state):
        if len(self.saved)+1==self.fail_save:
            raise StateError("simulierter Speicherfehler")
        self.value=copy.deepcopy(state)
        self.saved.append(copy.deepcopy(state))


class RunTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.root=Path(self.folder.name)
        self.patch=patch.object(cloud_run,"ROOT",self.root)
        self.patch.start()
        self.env=patch.dict(os.environ,{"INITIAL_PREVIEW_STATE":""})
        self.env.start()
        self.refresh_calls=0
        self.posts=0
    async def asyncTearDown(self):
        self.env.stop()
        self.patch.stop()
        self.folder.cleanup()

    async def refresh(self,news,report,*,now):
        self.refresh_calls+=1
        atomic_json_write(news,payload(now))
        return {"status":"matched","sources":{"Investing.com":{"ok":True},"Forex Factory":{"ok":True}}}

    async def post(self,news,state,*,preview,refresh_existing):
        self.posts+=1
        data=json.loads(news.read_text(encoding="utf-8"))
        atomic_json_write(state,{"last_sent_week":data["week_start"],"message_id":101,
                                "format_version":2,"preview":preview,"cleanup_message_ids":[]})
        return "1 Wochenkarte versendet."

    async def test_scheduled_post_persists_and_rerun_sends_nothing(self):
        store=Store()
        first=await cloud_run.execute("send",store,now=MONDAY,publisher=self.post,refresher=self.refresh)
        self.assertEqual(first["status"],"published")
        self.assertEqual(first["messages"],1)
        self.assertIn("reservation",store.saved[0])
        self.assertNotIn("reservation",store.value)
        self.assertEqual(store.value["production"]["message_id"],101)
        second=await cloud_run.execute("send",store,now=MONDAY,publisher=self.post,refresher=self.refresh)
        self.assertEqual(second["status"],"already_sent")
        self.assertEqual((self.posts,self.refresh_calls),(1,1))

    async def test_failed_reservation_never_contacts_discord(self):
        store=Store(fail_save=1)
        with self.assertRaises(StateError):
            await cloud_run.execute("send",store,now=MONDAY,publisher=self.post,refresher=self.refresh)
        self.assertEqual(self.posts,0)

    async def test_uncertain_delivery_is_not_repeated(self):
        store=Store()
        async def fail(*args,**kwargs):
            self.posts+=1
            raise RuntimeError("simulierter Abbruch")
        with self.assertRaises(RuntimeError):
            await cloud_run.execute("send",store,now=MONDAY,publisher=fail,refresher=self.refresh)
        with self.assertRaises(StateError):
            await cloud_run.execute("send",store,now=MONDAY,publisher=self.post,refresher=self.refresh)
        self.assertEqual(self.posts,1)

    async def test_failed_final_save_does_not_send_again(self):
        store=Store(fail_save=2)
        with self.assertRaises(StateError):
            await cloud_run.execute("send",store,now=MONDAY,publisher=self.post,refresher=self.refresh)
        self.assertEqual(self.posts,1)
        self.assertIn("reservation",store.value)
        with self.assertRaises(StateError):
            await cloud_run.execute("send",store,now=MONDAY,publisher=self.post,refresher=self.refresh)
        self.assertEqual(self.posts,1)

    async def test_sources_blocked_means_no_reservation_or_message(self):
        store=Store()
        async def blocked(*args,**kwargs):
            return {"status":"blocked","sources":{"Investing.com":{"ok":False}}}
        result=await cloud_run.execute("send",store,now=MONDAY,publisher=self.post,refresher=blocked)
        self.assertEqual(result["status"],"sources_blocked")
        self.assertEqual(store.saved,[])
        self.assertEqual(self.posts,0)

    async def test_cloud_preview_updates_existing_card_and_preserves_production(self):
        production={"last_sent_week":"2026-11-09","message_id":11}
        store=Store({"production":production,"preview":{"last_sent_week":"2026-11-16","message_id":12}})
        seen={}
        async def post(*args,**kwargs):
            seen.update(kwargs)
            return await self.post(*args,**kwargs)
        with patch.dict(os.environ,{"PREVIEW_PAYLOAD":json.dumps(payload(FRIDAY))}):
            result=await cloud_run.execute("trial",store,now=FRIDAY,publisher=post)
        self.assertEqual(result["status"],"published")
        self.assertTrue(seen["preview"])
        self.assertTrue(seen["refresh_existing"])
        self.assertEqual(store.value["production"],production)

    async def test_source_only_check_needs_no_discord_or_state(self):
        result=await cloud_run.execute("check",now=MONDAY,publisher=self.post,refresher=self.refresh)
        self.assertEqual(result["status"],"checked")
        self.assertEqual(self.posts,0)

    async def test_production_never_posts_outside_monday_after_six(self):
        store=Store()
        for now in (FRIDAY,datetime(2026,11,16,5,59,tzinfo=BERLIN)):
            result=await cloud_run.execute("send",store,now=now,publisher=self.post,refresher=self.refresh)
            self.assertEqual(result["status"],"outside_schedule")
        self.assertEqual(store.loads,0)
        self.assertEqual(self.posts,0)

    async def test_summer_and_winter_target_use_berlin_time(self):
        self.assertTrue(cloud_run.scheduled_time(datetime(2026,11,16,5,0,tzinfo=timezone.utc)))
        self.assertFalse(cloud_run.scheduled_time(datetime(2026,11,16,4,59,tzinfo=timezone.utc)))
        self.assertTrue(cloud_run.scheduled_time(datetime(2027,3,29,4,0,tzinfo=timezone.utc)))
        self.assertFalse(cloud_run.scheduled_time(datetime(2027,3,29,3,59,tzinfo=timezone.utc)))

    async def test_allowed_fallback_can_publish_once_and_does_not_claim_two_sources(self):
        async def fallback(news,report,*,now):
            raw=payload(now)
            raw["crosscheck"]={"status":"forex_factory_fallback","sources":["Forex Factory"],
                              "unavailable":{"Investing.com":"HTTP 403"}}
            atomic_json_write(news,raw)
            return {"status":"forex_factory_fallback","sources":{"Investing.com":{"ok":False},"Forex Factory":{"ok":True}}}
        store=Store()
        with patch.dict(os.environ,{"ALLOW_FOREX_FACTORY_FALLBACK":"true"}):
            result=await cloud_run.execute("send",store,now=MONDAY,publisher=self.post,refresher=fallback)
            checked=await cloud_run.execute("check",now=MONDAY,refresher=fallback)
        self.assertEqual(result["status"],"published")
        self.assertEqual(self.posts,1)
        self.assertEqual(checked["crosscheck"],"forex_factory_fallback")
        self.assertEqual(checked["day_sections"],5)
        self.assertEqual(checked["events"],0)
        with patch.dict(os.environ,{"ALLOW_FOREX_FACTORY_FALLBACK":"false"}):
            with self.assertRaises(cloud_run.NewsError):
                await cloud_run.execute("send",Store(),now=MONDAY,publisher=self.post,refresher=fallback)
        self.assertEqual(self.posts,1)
