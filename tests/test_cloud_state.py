import base64
import json
import unittest
from cryptography.fernet import Fernet

from cloud_state import GitHubState, StateError


class Response:
    def __init__(self, status, data=None):
        self.status, self.data = status, data
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    async def json(self):
        return self.data


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.responses.pop(0)
    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)
    def put(self, url, **kwargs):
        return self.request("PUT", url, **kwargs)


class StateTests(unittest.IsolatedAsyncioTestCase):
    def new(self, session=None, repo="owner/bot", key=None):
        return GitHubState(session or Session(), repo, "test-token", key or Fernet.generate_key().decode())

    async def test_encrypted_roundtrip_does_not_expose_message_id(self):
        store = self.new()
        state = {"production":{"message_id":123456789012345678, "last_sent_week":"2026-11-16"}}
        blob = store.encode(state)
        self.assertNotIn(b"123456789012345678", blob)
        self.assertNotIn(b"last_sent_week", blob)
        self.assertEqual(store.decode(blob), state)
        self.assertNotEqual(store.encode(state), blob)

    async def test_wrong_key_and_wrong_repository_are_rejected(self):
        key = Fernet.generate_key().decode()
        original = self.new(key=key)
        blob = original.encode({"preview":{"message_id":12}})
        with self.assertRaises(StateError):
            self.new().decode(blob)
        with self.assertRaises(StateError):
            self.new(key=key,repo="different/bot").decode(blob)

    async def test_load_missing_state_is_empty(self):
        store = self.new(Session(Response(404)))
        self.assertEqual(await store.load(), {})
        self.assertIsNone(store.sha)

    async def test_load_auth_failure_is_not_empty_state(self):
        store = self.new(Session(Response(403)))
        with self.assertRaises(StateError):
            await store.load()

    async def test_save_uses_ciphertext_and_existing_sha(self):
        session = Session(Response(200, {"content":{"sha":"newsha"}}))
        store = self.new(session)
        store.sha = "oldsha"
        state = {"reservation":{"week":"2026-11-16","slot":"production"}}
        await store.save(state)
        body = session.requests[0][2]["json"]
        self.assertEqual(body["sha"], "oldsha")
        self.assertEqual(store.decode(base64.b64decode(body["content"])), state)
        self.assertEqual(store.sha, "newsha")

    async def test_conflicting_state_is_not_overwritten(self):
        store = self.new()
        other = {"production":{"message_id":88}}
        remote = {"sha":"other","content":base64.b64encode(store.encode(other)).decode()}
        session = Session(Response(409), Response(200,remote))
        store.session = session
        with self.assertRaises(StateError):
            await store.save({"production":{"message_id":99}})
        self.assertEqual([item[0] for item in session.requests], ["PUT","GET"])

    async def test_already_committed_identical_state_is_accepted(self):
        store = self.new()
        desired = {"production":{"message_id":99}}
        remote = {"sha":"same","content":base64.b64encode(store.encode(desired)).decode()}
        store.session = Session(Response(409), Response(200,remote))
        await store.save(desired)
        self.assertEqual(store.sha, "same")
