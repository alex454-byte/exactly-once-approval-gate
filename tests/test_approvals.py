"""Offline tests with synthetic fixtures. No network, no subprocesses, no real tokens.

Each test uses its own temporary state directory (left in place for inspection).
"""
import concurrent.futures
import datetime as dt
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import contextlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gate"))
import approvals as a

SHA = "a" * 40
OTHER_SHA = "b" * 40
NOW = dt.datetime(2026, 10, 6, 12, 41, tzinfo=dt.timezone.utc)
OWNER = 987654321098
CHAT = 987654321099
FOREIGN = 876543210987


class FakeTelegram:
    def __init__(self):
        self.token = "FAKE-TOKEN-DO-NOT-LOG"
        self.calls = []
        self.updates = []
        self.fail_send = False
        self.fail_answer = False
        self.fail_edit = False

    def send_message(self, text, buttons=None):
        self.calls.append(("send", text, buttons))
        if self.fail_send:
            raise a.TelegramError()
        return 321

    def edit_message(self, message_id, text):
        self.calls.append(("edit", message_id, text))
        if self.fail_edit:
            raise a.TelegramError()

    def answer_callback(self, cb_id, text):
        self.calls.append(("answer", cb_id, text))
        if self.fail_answer:
            raise a.TelegramError()

    def get_updates(self, offset, timeout):
        self.calls.append(("get", offset, timeout))
        if isinstance(self.updates, Exception):
            raise self.updates
        return self.updates


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp(prefix="approval-case-"))
        self.env = mock.patch.dict(os.environ, {"APPROVALS_STATE_DIR": str(self.state),
                                                "APPROVALS_DECISIONS_LOG": str(self.state / "decisions.md"),
                                                "APPROVALS_HEARTBEAT": "", "APPROVALS_TZ": "America/New_York",
                                                "APPROVALS_TG_TOKEN": "FAKE-TOKEN-DO-NOT-LOG"})
        self.env.start()
        self.addCleanup(self.env.stop)
        a.atomic_write_json(self.state / "config.json", dict(owner_user_id=OWNER, chat_id=CHAT))
        self.tg = FakeTelegram()
        self.launched = []
        self.ctx = a.Context(a.load_config(), self.tg, lambda: NOW, lambda: SHA,
                             lambda argv, log, token: self.launched.append((argv, log)))
        # Any accidental external command or HTTP access fails the test immediately.
        for target in ("approvals.subprocess.run", "approvals.subprocess.Popen", "approvals.urllib.request.urlopen"):
            patcher = mock.patch(target, side_effect=AssertionError("External operation forbidden in offline tests"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def payload(self, sha=SHA, commits=2):
        return dict(sha=sha, build_number_expected=102, last_uploaded_build=100,
                    app_commits=commits, prs=[dict(number=1234, title="App changes")])

    def request(self, kind="test-echo", now=NOW):
        r = a.create_request(kind, self.payload() if kind == "tester-upload" else {"note": "hello"}, now)
        return a.set_message_id(r["id"], 321)

    def update(self, r, action="a", **overrides):
        cb = dict(id="callback", data=a.callback_data(r["id"], action),
                  **{"from": {"id": OWNER}}, message=dict(message_id=321, chat=dict(id=CHAT, type="private")))
        cb.update(overrides)
        return dict(update_id=15, callback_query=cb)

    def handle(self, update):
        before = len([x for x in self.tg.calls if x[0] == "answer"])
        a.handle_update(update, self.ctx)
        answers = [x for x in self.tg.calls if x[0] == "answer"]
        self.assertEqual(len(answers), before + 1)
        return answers[-1][2]

    def events(self):
        path = self.state / "audit.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_owner_approves_once_and_edits_local_time(self):
        r = self.request()
        self.assertEqual(self.handle(self.update(r)), "approved")
        self.assertEqual(a.load_request(r["id"])["status"], "approved")
        self.assertFalse(self.launched)
        self.ctx.clock = lambda: NOW + dt.timedelta(seconds=121)
        a.launch_due(self.ctx)
        self.assertEqual(len(self.launched), 1)
        text = next(c[2] for c in self.tg.calls if c[0] == "edit")
        self.assertIn("APPROVED by owner at 08:41 EDT", text)

    def test_foreign_user_hash_only(self):
        r = self.request()
        reply = self.handle(self.update(r, **{"from": {"id": FOREIGN}}))
        self.assertEqual(reply, "not authorised")
        self.assertEqual(a.load_request(r["id"])["status"], "pending")
        contents = (self.state / "audit.jsonl").read_text()
        self.assertNotIn(str(FOREIGN), contents)
        event = [e for e in self.events() if e["event"] == "callback_rejected"][-1]
        self.assertEqual(event["from_hash"], a.hash_id(FOREIGN))

    def test_wrong_chat_type_and_message(self):
        r = self.request()
        for chat, mid in [(dict(id=42, type="private"), 321), (dict(id=CHAT, type="group"), 321),
                          (dict(id=CHAT, type="private"), 999)]:
            with self.subTest(chat_type=chat["type"], mid=mid):
                self.assertEqual(self.handle(self.update(r, message=dict(chat=chat, message_id=mid))), "superseded — use the latest message" if mid == 999 else "not authorised")
                self.assertEqual(a.load_request(r["id"])["status"], "pending")
        self.assertFalse(self.launched)

    def test_double_tap(self):
        r = self.request()
        self.handle(self.update(r))
        self.assertIn("superseded", self.handle(self.update(r)))
        self.assertFalse(self.launched)
        self.ctx.clock = lambda: NOW + dt.timedelta(seconds=121)
        a.launch_due(self.ctx)
        self.assertEqual(len(self.launched), 1)

    def test_concurrent_decide_twenty_iterations(self):
        for iteration in range(20):
            r = self.request()
            barrier = threading.Barrier(2)
            def compete(status):
                barrier.wait()
                return a.decide(r["id"], status, "race", NOW)
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(compete, ["approved", "skipped"]))
            self.assertEqual(sum(won for won, _ in results), 1, iteration)
            winner = next(rec for won, rec in results if won)
            self.assertEqual(a.load_request(r["id"])["status"], winner["status"])

    def test_claim_crash_repair(self):
        r = self.request()
        decision = dict(status="approved", decided_at=a.iso(NOW), decided_reason="owner")
        a.atomic_write_json(a.paths().requests / (r["id"] + ".claim"), decision)
        repaired = a.load_request(r["id"])
        self.assertEqual(repaired["status"], "approved")
        self.assertFalse(a.decide(r["id"], "skipped", "owner", NOW)[0])
        self.assertEqual(json.loads((a.paths().requests / (r["id"] + ".json")).read_text())["status"], "approved")

    def test_expired(self):
        r = self.request(now=NOW - dt.timedelta(hours=13))
        self.assertIn("expired", self.handle(self.update(r)))
        self.assertEqual(a.load_request(r["id"])["status"], "expired")
        self.assertFalse(self.launched)

    def test_main_moved(self):
        r = self.request("tester-upload")
        self.ctx.main_sha = lambda: OTHER_SHA
        self.assertIn("main moved", self.handle(self.update(r)))
        loaded = a.load_request(r["id"])
        self.assertEqual((loaded["status"], loaded["decided_reason"]), ("expired", "main_moved"))
        self.assertFalse(self.launched)

    def test_resolver_failure_stays_pending(self):
        r = self.request("tester-upload")
        def fail():
            raise RuntimeError("failed")
        self.ctx.main_sha = fail
        self.assertEqual(self.handle(self.update(r)), "could not verify main, try again")
        self.assertEqual(a.load_request(r["id"])["status"], "pending")
        self.assertTrue(any(e.get("code") == "verify_failed" for e in self.events()))
        self.assertTrue(any(c[0] == "send" for c in self.tg.calls))

    def test_missing_repo_url_fails_closed(self):
        with mock.patch.dict(os.environ, {"APPROVALS_REPO_URL": ""}):
            with self.assertRaisesRegex(RuntimeError, "APPROVALS_REPO_URL"):
                a.main_sha(git_call=mock.Mock(side_effect=AssertionError("must not call git")))

    def test_kill_switch_absent_then_present(self):
        r = self.request("tester-upload")
        self.assertEqual(self.handle(self.update(r)), "approved")
        self.ctx.clock = lambda: NOW + dt.timedelta(seconds=121)
        a.launch_due(self.ctx)
        self.assertEqual(a.load_request(r["id"])["status"], "approved")
        self.assertFalse(self.launched)
        self.assertTrue(any("runner disabled, nothing built" in c[1] for c in self.tg.calls if c[0] == "send"))
        a.atomic_write_json(self.state / "tester-upload.enabled", True)
        r2 = self.request("tester-upload")
        self.handle(self.update(r2))
        self.assertFalse(self.launched)
        self.ctx.clock = lambda: NOW + dt.timedelta(seconds=242)
        a.launch_due(self.ctx)
        self.assertEqual(len(self.launched), 1)

    def test_skip_does_not_resolve_main(self):
        r = self.request("tester-upload")
        self.ctx.main_sha = mock.Mock(side_effect=AssertionError("Skip must not resolve main"))
        self.assertEqual(self.handle(self.update(r, "s")), "declined")
        self.assertEqual(a.load_request(r["id"])["status"], "declined")
        self.assertFalse(self.launched)

    def test_invalid_callbacks_and_id_path_guard(self):
        r = self.request()
        for data in [None, "", "ap1:../../x:a", "ap1:" + r["id"] + ":x", "ap1:" + r["id"] + ":a\n", "ap1:" + "z" * 22 + ":a"]:
            with self.subTest(data=data):
                self.assertEqual(self.handle(self.update(r, data=data)), "unknown request")
        with mock.patch.object(a, "paths", side_effect=AssertionError("Invalid IDs must not access paths")):
            for req_id in ["../../x", "x", "a" * 23, "a" * 22 + "\n"]:
                with self.assertRaises(ValueError):
                    a.load_request(req_id)
                with self.assertRaises(ValueError):
                    a.decide(req_id, "approved", "bad", NOW)

    def test_id_entropy_shape_and_callback_length(self):
        r = self.request()
        self.assertRegex(r["id"], r"^[A-Za-z0-9_-]{22}$")
        with mock.patch.object(a.secrets, "token_urlsafe", return_value="x" * 22) as token:
            a.create_request("test-echo", {}, NOW)
            token.assert_called_once_with(16)
        self.assertLessEqual(len(a.callback_data(r["id"], "a").encode()), 64)
        self.assertEqual(a.parse_callback(a.callback_data(r["id"], "s", 0)), (r["id"], "s", 0))

    def test_poison_update_offset_advances(self):
        r = self.request()
        first = self.update(r)
        second = dict(update_id=19, callback_query=dict(id="poison", data="bad"))
        third = dict(update_id=21)
        def handler(update, ctx):
            if update["update_id"] == 19:
                raise RuntimeError("poison")
            a.handle_update(update, ctx)
        self.assertEqual(a.process_batch([first, second, third], self.ctx, handler), 22)
        self.assertEqual(json.loads((self.state / "offset").read_text()), 22)
        self.assertTrue(any(e.get("reason") == "update_failed" for e in self.events()))

    def test_listener_once_persists_offset(self):
        r = self.request()
        self.tg.updates = [self.update(r), {"update_id": 22}]
        self.assertEqual(a.listener(self.ctx, once=True), 0)
        self.assertEqual(json.loads((self.state / "offset").read_text()), 23)
        self.assertIn(("get", 0, 0), self.tg.calls)

    def test_listener_backoff_alerts_after_five_failures(self):
        elapsed = [0.0]
        poll_times = []
        def poll(offset, timeout):
            poll_times.append(elapsed[0])
            raise a.TelegramError()
        def sleep(seconds):
            elapsed[0] += seconds
        self.tg.get_updates = poll
        self.assertEqual(a.listener(self.ctx, max_seconds=80, sleep=sleep,
                                    monotonic=lambda: elapsed[0]), 0)
        self.assertEqual(poll_times, [0, 5, 15, 35, 75])
        self.assertEqual(len([c for c in self.tg.calls if c[0] == "send"]), 1)

    def test_listener_success_heartbeat_at_most_every_five_minutes(self):
        elapsed = [0.0]
        def poll(offset, timeout):
            elapsed[0] += 100
            return []
        self.tg.get_updates = poll
        with mock.patch.object(a, "heartbeat") as beat:
            a.listener(self.ctx, max_seconds=500, monotonic=lambda: elapsed[0])
        self.assertEqual(beat.call_args_list, [mock.call("approval-listener", "success")] * 2)

    def test_listener_transient_failures_recover_immediately(self):
        elapsed = [0]
        results = iter([[], a.TelegramError(), a.TelegramError(), []])
        def poll(*args):
            elapsed[0] += 1
            result = next(results)
            if isinstance(result, Exception):
                raise result
            return result
        def sleep(seconds):
            elapsed[0] += seconds
        self.tg.get_updates = poll
        with mock.patch.object(a, "heartbeat") as beat:
            a.listener(self.ctx, max_seconds=19, sleep=sleep, monotonic=lambda: elapsed[0])
        self.assertEqual(beat.call_args_list, [mock.call("approval-listener", "success")] * 2)

    def test_listener_five_failures_write_one_fail(self):
        elapsed = [0]
        self.tg.updates = a.TelegramError()
        def sleep(seconds):
            elapsed[0] += seconds
        with mock.patch.object(a, "heartbeat") as beat:
            a.listener(self.ctx, max_seconds=80, sleep=sleep, monotonic=lambda: elapsed[0])
        self.assertEqual(beat.call_args_list, [mock.call("approval-listener", "fail", 1)])

    def test_callback_answer_failure_not_fatal(self):
        r = self.request()
        self.tg.fail_answer = True
        self.handle(self.update(r))
        self.assertFalse(self.launched)
        self.ctx.clock = lambda: NOW + dt.timedelta(seconds=121)
        a.launch_due(self.ctx)
        self.assertEqual(len(self.launched), 1)
        self.assertTrue(any(e["event"] == "callback_answer_failed" for e in self.events()))

    def test_unexpected_launcher_failure_still_answers_once(self):
        r = self.request()
        self.ctx.launcher = mock.Mock(side_effect=RuntimeError("cannot launch"))
        a.process_batch([self.update(r)], self.ctx)
        self.assertEqual(len([c for c in self.tg.calls if c[0] == "answer"]), 1)
        self.assertEqual(json.loads((self.state / "offset").read_text()), 16)

    def test_edit_failure_does_not_drop_runner(self):
        r = self.request()
        self.tg.fail_edit = True
        self.handle(self.update(r))
        self.assertFalse(self.launched)
        self.ctx.clock = lambda: NOW + dt.timedelta(seconds=121)
        a.launch_due(self.ctx)
        self.assertEqual(len(self.launched), 1)
        self.assertTrue(any(e["event"] == "message_edit_failed" for e in self.events()))

    def test_non_callback_ignored(self):
        a.handle_update({"message": {}}, self.ctx)
        self.assertEqual(self.tg.calls, [])

    def test_sweep(self):
        r = self.request(now=NOW - dt.timedelta(hours=13))
        a.sweep_expired(self.ctx, NOW)
        a.sweep_expired(self.ctx, NOW)
        self.assertEqual(a.load_request(r["id"])["status"], "expired")
        self.assertEqual(len([c for c in self.tg.calls if c[0] == "edit"]), 1)

    def test_sweep_skips_claim_before_loading(self):
        claimed = self.request(now=NOW - dt.timedelta(hours=13))
        (a.paths().requests / (claimed["id"] + ".claim")).write_text("")
        pending = self.request(now=NOW - dt.timedelta(hours=13))
        with mock.patch.object(a, "load_request", wraps=a.load_request) as load:
            a.sweep_expired(self.ctx, NOW)
        self.assertTrue(load.call_args_list)
        self.assertTrue(all(c == mock.call(pending["id"]) for c in load.call_args_list))
        self.assertEqual(a.load_request(pending["id"])["status"], "expired")

    def test_send_failure_expires_request(self):
        self.tg.fail_send = True
        with self.assertRaisesRegex(RuntimeError, "Could not send"):
            a.send_request("test-echo", {}, self.ctx)
        r = a.all_requests()[0]
        self.assertEqual((r["status"], r["decided_reason"]), ("expired", "send_failed"))

    def test_send_success_stores_message_and_buttons(self):
        r = a.send_request("test-echo", {"note": "<untrusted>"}, self.ctx)
        self.assertEqual(r["message_id"], 321)
        call = self.tg.calls[0]
        self.assertTrue(call[1].startswith("[TEST] "))
        self.assertEqual([b["text"] for b in call[2][0]], ["Yes", "No"])
        self.assertEqual(a.load_request(r["id"])["message_id"], 321)

    def test_incomplete_risk_block_expires_without_sending(self):
        with mock.patch.dict(a.KINDS, {"test-echo": a.KINDS["test-echo"].__class__(
                "test-echo", lambda p: "x", lambda r, c: (True, "ok", ""), lambda r: ["x"],
                lambda r, c: dict(production_risk="none"), "test")}):
            r = a.send_request("test-echo", {}, self.ctx)
        self.assertEqual((r["status"], r["decided_reason"]), ("expired", "risk_block_incomplete"))
        sends = [c for c in self.tg.calls if c[0] == "send"]
        self.assertEqual(len(sends), 1)
        self.assertIn("risk block incomplete", sends[0][1])
        self.assertIsNone(sends[0][2])

    def test_edited_expiry_reasons_do_not_attribute_owner(self):
        for reason, wording in [("timeout", "timeout"), ("main_moved", "main moved"),
                                ("superseded", "superseded"), ("send_failed", "send failed")]:
            with self.subTest(reason=reason):
                r = self.request()
                _, r = a.decide(r["id"], "expired", reason, NOW)
                a.edit_decision(r, self.ctx)
                text = self.tg.calls[-1][2]
                self.assertTrue(text.endswith(f"EXPIRED ({wording}) at 08:41 EDT"))
                self.assertNotIn("by owner", text)

    def test_token_and_chat_never_leak(self):
        token = "FAKE-TOKEN-DO-NOT-LOG"
        def transport(request, **kwargs):
            raise urllib.error.URLError(request.full_url + " chat=" + str(CHAT))
        tg = a.Telegram(self.ctx.config, token=token, transport=transport)
        with self.assertRaises(a.TelegramError) as raised:
            tg.send_message("hello", None)
        self.assertNotIn(token, str(raised.exception))
        self.assertNotIn(str(CHAT), str(raised.exception))
        a.audit("error", detail=token + " " + str(CHAT))
        text = (self.state / "audit.jsonl").read_text()
        self.assertNotIn(token, text)
        self.assertNotIn(str(CHAT), text)

    def test_http_409_scrubbed(self):
        def transport(request, **kwargs):
            raise urllib.error.HTTPError(request.full_url, 409, "FAKE-TOKEN-DO-NOT-LOG", {}, None)
        tg = a.Telegram(self.ctx.config, transport=transport)
        with self.assertRaises(a.TelegramError) as raised:
            tg.get_updates(0, 0)
        self.assertEqual(raised.exception.code, 409)
        self.assertNotIn("FAKE-TOKEN", str(raised.exception))
        self.tg.updates = raised.exception
        self.assertEqual(a.listener(self.ctx, once=True), 3)

    def test_transport_plain_text_truncation_and_keyboard_removal(self):
        requests = []
        def transport(request, **kwargs):
            requests.append(json.loads(request.data))
            return io.StringIO(json.dumps(dict(ok=True, result={"message_id": 321})))
        tg = a.Telegram(self.ctx.config, token="fake-token", transport=transport)
        tg.send_message("<b>" + "x" * 4000, None)
        tg.edit_message(321, "y" * 4000)
        tg.get_updates(12, 50)
        self.assertEqual(len(requests[0]["text"]), 3800)
        self.assertNotIn("parse_mode", requests[0])
        self.assertEqual(requests[1]["reply_markup"], {"inline_keyboard": []})
        self.assertEqual(requests[2]["allowed_updates"], ["callback_query"])

    def test_alert_hourly_and_send_failure_nonfatal(self):
        a.alert("test", "message", self.ctx)
        a.alert("test", "message", self.ctx)
        self.assertEqual(len([c for c in self.tg.calls if c[0] == "send"]), 1)
        self.ctx.clock = lambda: NOW + dt.timedelta(hours=1)
        a.alert("test", "message", self.ctx)
        self.assertEqual(len([c for c in self.tg.calls if c[0] == "send"]), 2)
        self.tg.fail_send = True
        with contextlib.redirect_stderr(io.StringIO()) as out:
            a.alert("failed", "message", self.ctx)
        self.assertIn("delivery failed", out.getvalue())
        self.assertTrue(any(e["event"] == "alert_failed" for e in self.events()))

    def test_alert_audit_storage_failure_is_nonfatal(self):
        self.tg.fail_send = True
        with mock.patch.object(a, "audit", side_effect=OSError("disk unavailable")), \
             contextlib.redirect_stderr(io.StringIO()) as out:
            a.alert("disk_failure", "message", self.ctx)
        self.assertIn("Could not write approval alert audit", out.getvalue())

    def test_authorisation_precedes_parsing_or_store_access(self):
        with mock.patch.object(a, "load_request", side_effect=AssertionError("Unauthorized read")):
            update = {"callback_query": {"id": "foreign", "data": "bad", "from": {"id": FOREIGN}}}
            self.assertEqual(self.handle(update), "not authorised")

    def test_missing_config_fails_without_network(self):
        empty = self.state / "missing"
        with mock.patch.dict(os.environ, {"APPROVALS_STATE_DIR": str(empty)}):
            with self.assertRaisesRegex(RuntimeError, "Missing approvals config.json"):
                a.context()

    def test_missing_token_fails_before_network(self):
        with mock.patch.dict(os.environ, {"APPROVALS_TG_TOKEN": ""}):
            with self.assertRaisesRegex(RuntimeError, "APPROVALS_TG_TOKEN"):
                a.context()

    def test_permissions_and_atomic_json(self):
        r = self.request()
        a.decide(r["id"], "approved", "owner", NOW)
        for path in self.state.rglob("*"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600, str(path))
        path = self.state / "replace.json"
        a.atomic_write_json(path, {"one": 1})
        a.atomic_write_json(path, {"two": 2})
        self.assertEqual(json.loads(path.read_text()), {"two": 2})

    def test_ledger_records_request_and_changed_decision_once(self):
        r = a.send_request("test-echo", {"note": "x"}, self.ctx)
        tap = self.update(r)
        self.handle(tap)
        r = a.load_request(r["id"])
        cb = dict(id="cb", data=a.callback_data(r["id"], "n", r["version"]), **{"from": {"id": OWNER}},
                  message=dict(message_id=r["live_message_id"], chat=dict(id=CHAT, type="private")))
        self.handle(dict(update_id=16, callback_query=cb))
        a.load_request(r["id"])  # Replays must not duplicate ledger rows.
        rows = [json.loads(line) for line in (self.state / "ledger.jsonl").read_text().splitlines()]
        self.assertEqual([row["event"] for row in rows], ["requested", "decided", "decided"])
        self.assertEqual((rows[-1]["first_decision"], rows[-1]["final_decision"], rows[-1]["changed"]), ("yes", "no", True))


class VersionedDecisionTests(unittest.TestCase):
    """Change-your-answer, grace period and race behaviour."""
    setUp = ApprovalTests.setUp
    payload = ApprovalTests.payload
    request = ApprovalTests.request
    events = ApprovalTests.events

    def prepare(self, kind='tester-upload'):
        self.now = NOW
        self.ctx.clock = lambda: self.now
        self.mid = 400
        def send(text, buttons=None):
            self.mid += 1
            self.tg.calls.append(('send', text, buttons))
            return self.mid
        self.tg.send_message = send
        (self.state / 'tester-upload.enabled').write_text('yes')
        return self.request(kind)

    def tap(self, r, action, version=None, message=None, owner=OWNER):
        data = a.callback_data(r['id'], action, r.get('version', 0) if version is None else version)
        cb = {'id': 'cb', 'data': data, 'from': {'id': owner}, 'message': {'message_id': r.get('live_message_id', r['message_id']) if message is None else message, 'chat': {'id': CHAT, 'type': 'private'}}}
        a.handle_update({'callback_query': cb}, self.ctx)
        return a.load_request(r['id'])

    def test_confirmation_and_buttons(self):
        r = self.prepare()
        for action, choice, button in [('a', 'YES', 'Change to No'), ('n', 'NO', 'Change to Yes')]:
            r = self.tap(r, action)
            text, buttons = [(c[1], c[2]) for c in self.tg.calls if c[0] == 'send'][-1]
            for value in ['You said ' + choice, '+102', SHA[:8], '…' + r['id'][-6:]]:
                self.assertIn(value, text)
            self.assertEqual(buttons[0][0]['text'], button)
            self.assertLessEqual(len(buttons[0][0]['callback_data'].encode()), 64)
        self.assertTrue(any('superseded' in c[2] for c in self.tg.calls if c[0] == 'edit'))
        self.assertFalse(self.launched)

    def test_grace_reversal_restart(self):
        r = self.prepare('test-echo'); r = self.tap(r, 'a')
        self.assertEqual(a.stamp(r['launch_at']), NOW + dt.timedelta(seconds=120))
        self.assertFalse(self.launched)
        r = self.tap(r, 'n'); self.now += dt.timedelta(seconds=121)
        a.listener(self.ctx, once=True); self.assertFalse(self.launched)
        r = self.tap(r, 'y'); self.assertEqual(a.stamp(r['launch_at']), self.now + dt.timedelta(seconds=120))
        self.now += dt.timedelta(seconds=121)
        a.listener(self.ctx, once=True); a.listener(self.ctx, once=True)
        self.assertEqual(len(self.launched), 1)
        self.assertTrue(a.load_request(r['id'])['launched'])

    def test_reapprove_expiry_and_main_moved(self):
        r = self.prepare(); r = self.tap(r, 's')
        self.ctx.main_sha = lambda: OTHER_SHA
        unchanged = self.tap(r, 'y'); self.assertEqual(unchanged['version'], r['version'])
        self.assertIn('fresh request', self.tg.calls[-1][2])
        self.now += dt.timedelta(hours=13)
        r = self.tap(r, 'y'); self.assertEqual(r['status'], 'expired')
        self.assertIn('fresh request', self.tg.calls[-1][2])

    def test_stale_foreign_and_double_reversal(self):
        old = self.prepare(); r = self.tap(old, 'a')
        for action in ['y', 'n']:
            self.assertEqual(self.tap(r, action, owner=FOREIGN)['version'], r['version'])
        for version, message in [(0, r['live_message_id']), (r['version'], 321)]:
            self.assertEqual(self.tap(r, 'n', version, message)['version'], r['version'])
            self.assertIn('superseded', self.tg.calls[-1][2])
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda _: self.tap(r, 'n'), range(2)))
        result = a.load_request(r['id'])
        self.assertEqual(result['version'], r['version'] + 1)
        self.assertEqual(result['status'], 'declined')

    def test_launch_cancel_race(self):
        r = self.prepare('test-echo'); r = self.tap(r, 'a'); self.now += dt.timedelta(seconds=121)
        barrier = threading.Barrier(2)
        def launch(): barrier.wait(); a.launch_due(self.ctx)
        def cancel(): barrier.wait(); self.tap(r, 'n')
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda fn: fn(), [launch, cancel]))
        final = a.load_request(r['id'])
        self.assertIn(final['status'], ['approved', 'declined'])
        self.assertEqual(len(self.launched), int(final['launched']))
        self.assertEqual(final['version'], 2)

    def test_legacy_and_claim_repair(self):
        r = self.prepare('test-echo')
        for key in ['version', 'history', 'live_message_id', 'launched', 'launch_at']:
            r.pop(key, None)
        a.atomic_write_json(a.paths().requests / (r['id'] + '.json'), r)
        cb = ApprovalTests.update(self, r)
        cb['callback_query']['data'] = 'ap1:' + r['id'] + ':a'
        a.handle_update(cb, self.ctx)
        r = a.load_request(r['id']); self.assertEqual(r['status'], 'approved')
        saved = dict(r)
        won, changed = a.transition(r['id'], r['version'], 'declined', 'owner', self.now)
        self.assertTrue(won)
        a.atomic_write_json(a.paths().requests / (r['id'] + '.json'), saved)
        self.assertEqual(a.load_request(r['id'])['history'], changed['history'])
        for action in 'asyn':
            self.assertLessEqual(len(a.callback_data(r['id'], action, 999999).encode()), 64)

    def test_finished_and_cancel_requested_have_no_buttons(self):
        r = self.prepare(); r = self.tap(r, 'a'); self.now += dt.timedelta(seconds=121)
        a.launch_due(self.ctx); r = a.load_request(r['id']); r = self.tap(r, 'n')
        self.assertEqual(r['status'], 'cancel_requested')
        self.assertIsNone([c for c in self.tg.calls if c[0] == 'send'][-1][2])
        self.assertEqual(self.tap(r, 'y')['version'], r['version'])
        a.runner_outcome(r, self.ctx, 'cancelled before upload; nothing uploaded')
        finished = a.load_request(r['id'])
        self.assertEqual(finished['status'], 'finished')
        self.assertEqual(self.tap(finished, 'y')['version'], finished['version'])

    def test_cancel_claim_crash_repair(self):
        r = self.prepare(); r = self.tap(r, 'a'); self.now += dt.timedelta(seconds=121)
        a.launch_due(self.ctx); r = a.load_request(r['id'])
        claim = dict(version=r['version'] + 1, status='cancel_requested', decided_at=a.iso(self.now),
                     decided_reason='cancel requested before run', history=r['history'] +
                     [dict(version=r['version'] + 1, status='cancel_requested', at=a.iso(self.now), reason='cancel requested before run')])
        a.atomic_write_json(a.paths().requests / (r['id'] + f".v{claim['version']}.claim"), claim)
        repaired = a.load_request(r['id'])
        self.assertEqual(repaired['status'], 'cancel_requested')
        self.assertTrue((self.state / 'runs' / (r['id'] + '.cancel')).exists())

    def test_launch_failure_logged_and_never_relaunched(self):
        r = self.prepare('test-echo'); r = self.tap(r, 'a')
        self.ctx.launcher = mock.Mock(side_effect=RuntimeError('cannot launch'))
        self.now += dt.timedelta(seconds=121)
        a.launch_due(self.ctx); a.launch_due(self.ctx)
        self.ctx.launcher.assert_called_once()
        self.assertIn('launch failed', a.load_request(r['id'])['history'][-1]['reason'])
        self.assertIn('launch failed', (self.state / 'decisions.md').read_text())

    def test_due_batch_launches_before_telegram_io(self):
        r = self.prepare('test-echo'); self.tap(r, 'a')
        second = self.request('test-echo'); self.tap(second, 'a')
        self.now += dt.timedelta(seconds=121)
        original = self.tg.send_message
        def send(text, buttons=None):
            self.assertEqual(len(self.launched), 2)
            return original(text, buttons)
        self.tg.send_message = send
        a.launch_due(self.ctx)
        self.assertFalse(any(e['event'] == 'confirmation_send_failed' for e in self.events()))

    def test_incomplete_versioned_claim_fails_closed(self):
        r = self.prepare('test-echo')
        (a.paths().requests / (r['id'] + '.v1.claim')).write_text('')
        with self.assertRaisesRegex(RuntimeError, 'Incomplete versioned claim'):
            a.load_request(r['id'])
        self.assertFalse(self.launched)

    def test_reversals_enforce_chat_and_private_checks(self):
        r = self.prepare('test-echo'); r = self.tap(r, 'a')
        for action in ['y', 'n']:
            for chat in [{'id': FOREIGN, 'type': 'private'}, {'id': CHAT, 'type': 'group'}]:
                cb = {'id': 'cb', 'data': a.callback_data(r['id'], action, r['version']),
                      'from': {'id': OWNER}, 'message': {'message_id': r['live_message_id'], 'chat': chat}}
                a.handle_update({'callback_query': cb}, self.ctx)
                self.assertEqual(self.tg.calls[-1][2], 'not authorised')
                self.assertEqual(a.load_request(r['id'])['version'], r['version'])

    def test_accepted_tap_still_confirms_if_runner_finishes_first(self):
        r = self.prepare(); r = self.tap(r, 'a'); self.now += dt.timedelta(seconds=121)
        a.launch_due(self.ctx); r = a.load_request(r['id'])
        original = a.confirm
        def raced(record, ctx, text=None):
            a.runner_outcome(record, ctx, 'cancelled before upload; nothing uploaded')
            original(record, ctx, text)
        before = len([c for c in self.tg.calls if c[0] == 'send'])
        with mock.patch.object(a, 'confirm', side_effect=raced):
            self.tap(r, 'n')
        messages = [c for c in self.tg.calls if c[0] == 'send']
        self.assertEqual(len(messages), before + 1)
        self.assertIn('You said NO', messages[-1][1])
        self.assertIsNone(messages[-1][2])
        self.assertEqual(a.load_request(r['id'])['status'], 'finished')


if __name__ == "__main__":
    unittest.main()
