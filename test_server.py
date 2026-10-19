import concurrent.futures
import tempfile
import unittest
import io
import json
import hmac
import hashlib
import threading
from urllib.request import Request, urlopen
from pathlib import Path
from unittest.mock import patch
import server
from usage import Tracker, totals
from credits import award, account, schema as credit_schema, SCALE
from contributions import import_events


class ProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(server, "STATE", Path(self.tmp.name))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_concurrent_first_requests_create_once(self):
        calls = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            profiles = list(pool.map(lambda _: server.ensure_user({"id": "alice"}, calls.append), range(8)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(set(profiles)), 1)

    def test_users_are_separate_and_body_cannot_choose_profile(self):
        a = server.ensure_user({"id": "alice", "profile": "default"}, lambda p: None)
        b = server.ensure_user({"id": "bob"}, lambda p: None)
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("owui-"))

    def test_failed_setup_can_retry(self):
        def fail(p):
            raise RuntimeError("unavailable")
        with self.assertRaises(RuntimeError):
            server.ensure_user({"id": "alice"}, fail)
        server.ensure_user({"id": "alice"}, lambda p: None)
        with server.connect() as db:
            self.assertEqual(db.execute("select status from users").fetchone()[0], "ready")

    def test_missing_user_rejected(self):
        with self.assertRaises(ValueError):
            server.ensure_user({}, lambda p: self.fail("must not provision"))

    def test_stream_usage_split_across_chunks_counted_once(self):
        server.ensure_user({'id': 'alice'}, lambda p: None)
        t = Tracker(server.connect, 'alice', True)
        for part in (b'data: {"usa', b'ge":{"prompt_tokens":12,"completion_tokens":3,',
                     b'"total_tokens":15}}\n\ndata: [DONE]\n\n'):
            t.observe(part)
        t.finish()
        t.finish()
        with server.connect() as db:
            result = totals(db, 'alice')
        self.assertEqual(result[:4], (1, 12, 3, 15))
        self.assertIsNone(result[4])

    def test_missing_usage_is_unknown(self):
        server.ensure_user({'id': 'alice'}, lambda p: None)
        t = Tracker(server.connect, 'alice', False)
        t.observe(b'{"choices":[]}')
        t.finish()
        with server.connect() as db:
            row = db.execute('select status,total_tokens,credits_microunits from usage_events').fetchone()
        self.assertEqual(row, ('usage_unreported', None, None))

    def test_live_formula_deducts_and_allows_negative_balance(self):
        server.ensure_user({'id': 'alice'}, lambda p: None)
        t = Tracker(server.connect, 'alice', False, {'formula': 'tokens100', 'version': 'v1'})
        t.observe(b'{"usage":{"prompt_tokens":1588,"completion_tokens":3,"total_tokens":1591}}')
        t.finish()
        t.finish()
        with server.connect() as db:
            a = account(db, 'alice')
        self.assertEqual(a['spent_microunits'], 16*SCALE)
        self.assertEqual(a['balance_microunits'], -16*SCALE)

    def test_reward_import_uses_linked_device_and_is_idempotent(self):
        server.ensure_user({'id': 'alice'}, lambda p: None)
        events = [{'id': 'event-1', 'device_id': 'device-1', 'amount': '3.25', 'entry_type': 'job_reward'}]
        with server.connect() as db:
            credit_schema(db)
            self.assertEqual(import_events(db, events, 'https://trusted/credits'), 0)
            db.execute('INSERT INTO contributor_devices VALUES (?,?)', ('device-1', 'alice'))
            db.execute('INSERT INTO user_credit_roles VALUES (?,1)', ('alice',))
            self.assertEqual(import_events(db, events, 'https://trusted/credits'), 1)
            self.assertEqual(import_events(db, events, 'https://trusted/credits'), 0)
            self.assertEqual(account(db, 'alice')['balance_microunits'], 3_250_000)

    def test_noncontributor_cannot_receive_contribution_award(self):
        server.ensure_user({'id': 'alice'}, lambda p: None)
        with server.connect() as db:
            with self.assertRaises(ValueError):
                award(db, 'alice', 3, 'contribution', 'work', 'event-1')

    def test_reference_cannot_be_reused_for_different_award(self):
        server.ensure_user({'id': 'alice'}, lambda p: None)
        with server.connect() as db:
            award(db, 'alice', 3, 'grant', 'grant', 'event-1')
            with self.assertRaises(ValueError):
                award(db, 'alice', 9, 'grant', 'grant', 'event-1')

    def test_zero_usage_for_nonempty_prompt_is_unrated(self):
        server.ensure_user({'id': 'alice'}, lambda p: None)
        t = Tracker(server.connect, 'alice', False, {'formula': 'tokens100', 'version': 'v1'}, 20)
        t.observe(b'{"usage":{"prompt_tokens":0,"completion_tokens":0,"total_tokens":0}}')
        t.finish()
        with server.connect() as db:
            self.assertEqual(db.execute('select status,prompt_tokens,credits_microunits from usage_events').fetchone(), ('usage_unreported', None, None))

    def test_existing_character_fallback_is_labeled(self):
        server.ensure_user({'id': 'alice'}, lambda p: None)
        t = Tracker(server.connect, 'alice', False, {'formula': 'tokens100', 'version': 'fallback-v1', 'fallback_formula': 'chars400'}, 23)
        t.observe(json.dumps({'choices': [{'message': {'content': 'x'*835}}]}).encode())
        t.finish()
        with server.connect() as db:
            self.assertEqual(db.execute('select credits_microunits,billing_source from usage_events').fetchone(), (3*SCALE, 'character_estimate'))

    def test_direct_provider_counts_override_missing_hermes_metering(self):
        server.ensure_user({'id': 'alice'}, lambda p: None)
        t = Tracker(server.connect, 'alice', False, {'formula': 'tokens100', 'version': 'v1'}, 20)
        t.observe(b'{"usage":{"prompt_tokens":0,"completion_tokens":0,"total_tokens":0}}')
        t.add_provider_usage({'prompt_tokens': 150, 'completion_tokens': 10, 'total_tokens': 160})
        t.add_provider_usage({'prompt_tokens': 60, 'completion_tokens': 1, 'total_tokens': 61})
        t.finish()
        with server.connect() as db:
            self.assertEqual(db.execute('select prompt_tokens,completion_tokens,credits_microunits from usage_events').fetchone(), (210, 11, 3*SCALE))

    def test_provider_adapter_supplies_usage_and_tool_call_indexes(self):
        profile = server.ensure_user({'id': 'alice'}, lambda p: None)
        service = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        service.settings = {'upstream_key': 'a'*64, 'model_upstream': 'https://provider/v1'}
        response = {'id': 'test', 'created': 1, 'model': 'test', 'usage': {
            'prompt_tokens': 101, 'completion_tokens': 4, 'total_tokens': 105},
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'tool_calls': [
                {'id': 'call1', 'type': 'function', 'function': {'name': 'test', 'arguments': '{}'}}]}, 'finish_reason': 'tool_calls'}]}
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        key = hmac.new(b'a'*64, profile.encode(), hashlib.sha256).hexdigest()
        try:
            with patch.object(server, 'urlopen', return_value=io.BytesIO(json.dumps(response).encode())) as upstream:
                req = Request('http://127.0.0.1:' + str(service.server_port) + '/provider/' + profile + '/v1/chat/completions',
                              data=b'{"stream":true}', headers={'Authorization': 'Bearer '+key})
                with urlopen(req) as result:
                    events = [json.loads(line[6:]) for line in result.read().splitlines() if line.startswith(b'data: {')]
                self.assertEqual(events[-1]['usage']['total_tokens'], 105)
                self.assertEqual(events[0]['choices'][0]['delta']['tool_calls'][0]['index'], 0)
                self.assertFalse(json.loads(upstream.call_args.args[0].data)['stream'])
        finally:
            service.shutdown()
            service.server_close()


if __name__ == "__main__":
    unittest.main()
