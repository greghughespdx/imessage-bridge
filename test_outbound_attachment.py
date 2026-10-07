#!/usr/bin/env python3
"""
Tests for outbound image attachments on POST /send (mc-am50p).

No test here runs osascript: send_message is replaced with a recorder so the
suite never touches Messages.app or sends a real iMessage. The AppleScript that
WOULD have run is asserted as text instead.

Run: python3 -m pytest test_outbound_attachment.py
"""

import base64
import http.client
import json
import logging
import os
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

import bridge
from test_attributed_body import make_blob as make_attributed_body_blob


# Smallest thing that is unambiguously a PNG by signature and extension. The
# bridge does not parse image bytes, so content beyond this is irrelevant.
# Throwaway value. Never a real token, and never read from disk (mc-btl9u).
TEST_TOKEN = "test-token-not-a-real-secret"

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
)

MINIMAL_SCHEMA = """
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT, style INTEGER);
CREATE TABLE message (
    ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT, attributedBody BLOB,
    date INTEGER, is_from_me INTEGER, cache_has_attachments INTEGER DEFAULT 0,
    handle_id INTEGER, service TEXT, is_sent INTEGER DEFAULT 0,
    error INTEGER DEFAULT 0
);
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
CREATE TABLE attachment (
    ROWID INTEGER PRIMARY KEY, guid TEXT, filename TEXT, mime_type TEXT,
    transfer_name TEXT, uti TEXT, total_bytes INTEGER,
    transfer_state INTEGER DEFAULT 0, is_outgoing INTEGER DEFAULT 0
);
CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
"""


class BuildSendScriptTest(unittest.TestCase):
    def test_text_only_script_is_the_unchanged_one_liner(self):
        # The live text path must not change shape. This is exactly what
        # master (3ce9c66) sent, and what iMac27 runs today.
        script = bridge.build_send_script("iMessage;-;+15550001111", "hello", None)
        self.assertEqual(
            script,
            'tell application "Messages" to send "hello" '
            'to chat id "iMessage;-;+15550001111"',
        )
        self.assertNotIn("POSIX file", script)

    def test_attachment_follows_the_text(self):
        # This is the exact shape that delivered live test 1 on 2026-09-09
        # (attachment row 19396, transfer_state 5). Do not change it.
        script = bridge.build_send_script("chat-x", "look at this", "/tmp/pic.png")
        lines = [line.strip() for line in script.splitlines()]
        self.assertEqual(
            lines,
            [
                'tell application "Messages"',
                'set targetChat to chat id "chat-x"',
                'send "look at this" to targetChat',
                'send POSIX file "/tmp/pic.png" to targetChat',
                "end tell",
            ],
        )

    def test_attachment_alone_when_text_is_empty(self):
        script = bridge.build_send_script("chat-x", "", "/tmp/pic.png")
        self.assertNotIn("send \"\" to targetChat", script)
        self.assertIn('send POSIX file "/tmp/pic.png" to targetChat', script)

    def test_quotes_and_backslashes_in_the_path_are_escaped(self):
        script = bridge.build_send_script("chat-x", "", '/tmp/a"b\\c.png')
        self.assertIn('send POSIX file "/tmp/a\\"b\\\\c.png" to targetChat', script)


class OutboxJanitorTest(unittest.TestCase):
    """sweep_outbox: bounded, name-matched, direct children only."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mc-am50p-janitor-")
        self.outbox = os.path.join(self.tmp, "outbox")
        os.makedirs(self.outbox, mode=0o700)
        self.now = 1_800_000_000.0
        self.day = 86400

    def _staged(self, name, age_days, hexpart="0123456789abcdef0123456789abcdef"):
        path = os.path.join(self.outbox, "%s-%s" % (hexpart, name))
        with open(path, "wb") as f:
            f.write(PNG_BYTES)
        os.utime(path, (self.now - age_days * self.day, self.now - age_days * self.day))
        return path

    def _sweep(self, **kw):
        return bridge.sweep_outbox(
            self.outbox, max_age_s=kw.pop("max_age_s", 30 * self.day),
            max_deletes=kw.pop("max_deletes", 50), now=self.now,
        )

    def test_deletes_only_expired_bridge_staged_files(self):
        old = self._staged("old.png", 31)
        fresh = self._staged("fresh.png", 29, hexpart="f" * 32)
        foreign = os.path.join(self.outbox, "mc-am50p-test1.png")
        with open(foreign, "wb") as f:
            f.write(PNG_BYTES)
        os.utime(foreign, (self.now - 400 * self.day, self.now - 400 * self.day))
        self.assertEqual(self._sweep(), 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))
        self.assertTrue(os.path.exists(foreign), "a file the bridge did not stage was deleted")

    def test_skips_symlinks_and_directories_even_with_matching_names(self):
        target = os.path.join(self.tmp, "victim.png")
        with open(target, "wb") as f:
            f.write(PNG_BYTES)
        link = os.path.join(self.outbox, "a" * 32 + "-link.png")
        os.symlink(target, link)
        sub = os.path.join(self.outbox, "b" * 32 + "-dir")
        os.makedirs(sub)
        old_ts = self.now - 100 * self.day
        os.utime(link, (old_ts, old_ts), follow_symlinks=False)
        os.utime(sub, (old_ts, old_ts))
        self.assertEqual(self._sweep(), 0)
        self.assertTrue(os.path.lexists(link))
        self.assertTrue(os.path.exists(target))
        self.assertTrue(os.path.isdir(sub))

    def test_bounded_per_sweep_oldest_first(self):
        paths = [self._staged("p%d.png" % i, 40 + i, hexpart=("%032x" % i)) for i in range(5)]
        self.assertEqual(self._sweep(max_deletes=2), 2)
        # Oldest two (largest age) are gone, the rest remain.
        self.assertFalse(os.path.exists(paths[4]))
        self.assertFalse(os.path.exists(paths[3]))
        for p in paths[:3]:
            self.assertTrue(os.path.exists(p))

    def test_missing_outbox_is_zero_not_an_error(self):
        self.assertEqual(
            bridge.sweep_outbox(os.path.join(self.tmp, "nope"), max_age_s=1, now=self.now), 0
        )

    def test_defaults_come_from_module_settings(self):
        self._staged("old.png", bridge.OUTBOX_RETENTION_DAYS + 1)
        orig = bridge.OUTBOX_DIR
        bridge.OUTBOX_DIR = self.outbox
        try:
            self.assertEqual(bridge.sweep_outbox(now=self.now), 1)
        finally:
            bridge.OUTBOX_DIR = orig


class SendMessageArgvTest(unittest.TestCase):
    """finding 3: the whole AppleScript used to be argv entry 3 of osascript.

    That put the message body - and, with mc-am50p, the host path of the image
    - in the process list, readable by `ps` for every user on that Mac for as
    long as the send took. It now goes in on stdin, which no other process can
    read. subprocess.run is replaced with a recorder, so nothing here executes
    osascript or sends anything.
    """

    CHAT = "iMessage;-;+15550001111"
    SECRET_BODY = "body-that-must-never-reach-the-process-list"
    SECRET_PATH = "/Users/greg/private-crop-that-must-not-reach-ps.png"

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def setUp(self):
        self.calls = []
        self._orig_run = bridge.subprocess.run

        def fake_run(argv, **kwargs):
            self.calls.append((list(argv), dict(kwargs)))
            return self._Result()

        bridge.subprocess.run = fake_run

    def tearDown(self):
        bridge.subprocess.run = self._orig_run

    def _send(self, text, attachment):
        bridge.send_message(self.CHAT, text, attachment)
        self.assertEqual(len(self.calls), 1)
        return self.calls[0]

    def test_argv_carries_neither_the_body_nor_the_attachment_path(self):
        argv, _ = self._send(self.SECRET_BODY, self.SECRET_PATH)
        joined = " ".join(argv)
        self.assertNotIn(self.SECRET_BODY, joined)
        self.assertNotIn(self.SECRET_PATH, joined)
        self.assertNotIn("-e", argv)
        self.assertEqual(argv[0], "osascript")

    def test_stdin_carries_the_intended_script(self):
        _, kwargs = self._send(self.SECRET_BODY, self.SECRET_PATH)
        self.assertEqual(
            kwargs.get("input"),
            bridge.build_send_script(self.CHAT, self.SECRET_BODY, self.SECRET_PATH),
        )
        self.assertIn(self.SECRET_BODY, kwargs["input"])
        self.assertIn(self.SECRET_PATH, kwargs["input"])
        self.assertTrue(kwargs.get("text"))

    def test_the_text_only_script_reaches_stdin_byte_identical(self):
        # Same guarantee as BuildSendScriptTest, asserted at the boundary that
        # actually feeds osascript.
        argv, kwargs = self._send("hello", None)
        self.assertNotIn("hello", " ".join(argv))
        self.assertEqual(
            kwargs.get("input"),
            'tell application "Messages" to send "hello" '
            'to chat id "iMessage;-;+15550001111"',
        )


class ValidateOutboundAttachmentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mc-am50p-")
        self.png = os.path.join(self.tmp, "pic.png")
        with open(self.png, "wb") as f:
            f.write(PNG_BYTES)

    def test_accepts_an_absolute_image_path(self):
        self.assertEqual(bridge.validate_outbound_attachment(self.png), self.png)

    def test_accepts_heic_which_mimetypes_does_not_know(self):
        heic = os.path.join(self.tmp, "shot.heic")
        with open(heic, "wb") as f:
            f.write(PNG_BYTES)
        self.assertEqual(bridge.validate_outbound_attachment(heic), heic)

    def test_rejects_a_relative_path(self):
        with self.assertRaises(bridge.AttachmentRejected) as ctx:
            bridge.validate_outbound_attachment("pic.png")
        self.assertIn("absolute path", str(ctx.exception))

    def test_rejects_a_missing_file(self):
        with self.assertRaises(bridge.AttachmentRejected) as ctx:
            bridge.validate_outbound_attachment(os.path.join(self.tmp, "gone.png"))
        self.assertIn("not a file", str(ctx.exception))

    def test_rejects_a_directory(self):
        with self.assertRaises(bridge.AttachmentRejected):
            bridge.validate_outbound_attachment(self.tmp)

    def test_rejects_a_non_image(self):
        doc = os.path.join(self.tmp, "secrets.txt")
        with open(doc, "w") as f:
            f.write("not a picture")
        with self.assertRaises(bridge.AttachmentRejected) as ctx:
            bridge.validate_outbound_attachment(doc)
        self.assertIn("must be an image", str(ctx.exception))

    def test_rejects_an_empty_file(self):
        empty = os.path.join(self.tmp, "empty.png")
        open(empty, "wb").close()
        with self.assertRaises(bridge.AttachmentRejected) as ctx:
            bridge.validate_outbound_attachment(empty)
        self.assertIn("empty", str(ctx.exception))

    def test_rejects_a_file_over_the_cap(self):
        big = os.path.join(self.tmp, "big.png")
        with open(big, "wb") as f:
            f.truncate(bridge.MAX_ATTACHMENT_BYTES + 1)
        with self.assertRaises(bridge.AttachmentRejected) as ctx:
            bridge.validate_outbound_attachment(big)
        self.assertIn("over the", str(ctx.exception))


class StageOutboundAttachmentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mc-am50p-outbox-")
        self._orig_outbox = bridge.OUTBOX_DIR
        bridge.OUTBOX_DIR = os.path.join(self.tmp, "outbox")

    def tearDown(self):
        bridge.OUTBOX_DIR = self._orig_outbox

    def test_writes_a_0600_file_in_a_0700_dir(self):
        staged = bridge.stage_outbound_attachment(
            base64.b64encode(PNG_BYTES).decode("ascii"), "crop.png"
        )
        try:
            self.assertTrue(os.path.isabs(staged))
            self.assertTrue(staged.startswith(bridge.OUTBOX_DIR + os.sep))
            with open(staged, "rb") as f:
                self.assertEqual(f.read(), PNG_BYTES)
            self.assertEqual(stat.S_IMODE(os.stat(staged).st_mode), 0o600)
            self.assertEqual(
                stat.S_IMODE(os.stat(bridge.OUTBOX_DIR).st_mode), 0o700
            )
        finally:
            bridge.discard_staged_attachment(staged)

    def test_a_pre_existing_0755_outbox_is_tightened_before_anything_is_staged(self):
        # finding 5: os.makedirs(mode=0o700, exist_ok=True) does nothing at all
        # to a directory that already exists, so a 0755 outbox stayed 0755 and
        # every user on the Mac could read the images staged in it.
        os.makedirs(bridge.OUTBOX_DIR, mode=0o755)
        os.chmod(bridge.OUTBOX_DIR, 0o755)  # umask-proof
        self.assertEqual(stat.S_IMODE(os.stat(bridge.OUTBOX_DIR).st_mode), 0o755)

        staged = bridge.stage_outbound_attachment(
            base64.b64encode(PNG_BYTES).decode("ascii"), "crop.png"
        )
        try:
            self.assertEqual(
                stat.S_IMODE(os.stat(bridge.OUTBOX_DIR).st_mode), 0o700
            )
            self.assertEqual(stat.S_IMODE(os.stat(staged).st_mode), 0o600)
        finally:
            bridge.discard_staged_attachment(staged)

    def test_a_pre_existing_0777_outbox_is_tightened_too(self):
        os.makedirs(bridge.OUTBOX_DIR)
        os.chmod(bridge.OUTBOX_DIR, 0o777)
        bridge.ensure_private_outbox_dir(bridge.OUTBOX_DIR)
        self.assertEqual(stat.S_IMODE(os.stat(bridge.OUTBOX_DIR).st_mode), 0o700)

    def test_an_outbox_that_is_a_file_is_refused(self):
        with open(bridge.OUTBOX_DIR, "w") as f:
            f.write("not a directory")
        with self.assertRaises(RuntimeError) as ctx:
            bridge.ensure_private_outbox_dir(bridge.OUTBOX_DIR)
        self.assertIn(bridge.OUTBOX_DIR, str(ctx.exception))

    def test_an_outbox_owned_by_another_uid_is_refused(self):
        # /tmp is root-owned and 1777. Nothing is written to it here.
        with self.assertRaises(RuntimeError) as ctx:
            bridge.ensure_private_outbox_dir("/tmp")
        self.assertIn("not by the bridge", str(ctx.exception))

    def test_staged_name_keeps_the_basename_only(self):
        staged = bridge.stage_outbound_attachment(
            base64.b64encode(PNG_BYTES).decode("ascii"), "../../evil.png"
        )
        try:
            self.assertEqual(os.path.dirname(staged), bridge.OUTBOX_DIR)
            self.assertTrue(os.path.basename(staged).endswith("-evil.png"))
        finally:
            bridge.discard_staged_attachment(staged)

    def test_rejects_a_missing_name(self):
        with self.assertRaises(bridge.AttachmentRejected) as ctx:
            bridge.stage_outbound_attachment(
                base64.b64encode(PNG_BYTES).decode("ascii"), None
            )
        self.assertIn("attachment_name is required", str(ctx.exception))

    def test_rejects_a_non_image_name(self):
        with self.assertRaises(bridge.AttachmentRejected) as ctx:
            bridge.stage_outbound_attachment(
                base64.b64encode(PNG_BYTES).decode("ascii"), "payload.sh"
            )
        self.assertIn("must be an image", str(ctx.exception))

    def test_rejects_invalid_base64(self):
        with self.assertRaises(bridge.AttachmentRejected) as ctx:
            bridge.stage_outbound_attachment("not base64 !!!", "crop.png")
        self.assertIn("not valid base64", str(ctx.exception))

    def test_rejects_empty_bytes(self):
        with self.assertRaises(bridge.AttachmentRejected) as ctx:
            bridge.stage_outbound_attachment("", "crop.png")
        self.assertIn("empty", str(ctx.exception))

    def test_discard_tolerates_a_missing_file(self):
        bridge.discard_staged_attachment(os.path.join(self.tmp, "never-existed.png"))
        bridge.discard_staged_attachment(None)


class SendRouteTest(unittest.TestCase):
    """POST /send over a real loopback socket, with osascript stubbed out."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mc-am50p-http-")
        self.db_path = os.path.join(self.tmp, "chat.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(MINIMAL_SCHEMA)
        conn.commit()
        conn.close()

        self.png = os.path.join(self.tmp, "pic.png")
        with open(self.png, "wb") as f:
            f.write(PNG_BYTES)

        self._orig_outbox = bridge.OUTBOX_DIR
        bridge.OUTBOX_DIR = os.path.join(self.tmp, "outbox")

        # Record what would have been sent; never call osascript.
        self.sends = []
        self._orig_send = bridge.send_message

        def fake_send(chat_id, text, attachment_path=None):
            existed = bool(attachment_path) and os.path.isfile(attachment_path)
            self.sends.append(
                {
                    "chat_id": chat_id,
                    "text": text,
                    "attachment_path": attachment_path,
                    "attachment_existed_at_send": existed,
                }
            )
            return 0.01

        bridge.send_message = fake_send

        # mc-mnvrm: the route now waits on chat.db for the attachment's
        # transfer_state. Default it to "delivered, copied by Messages"; the
        # failure tests swap in other outcomes.
        self._orig_wait = bridge.wait_for_attachment_transfer
        self.waits = []
        self.wait_result = {
            "outcome": "sent",
            "detail": "transfer_state 5",
            "transfer_state": 5,
            "attachment_rowid": 42,
            "filename": "~/Library/Messages/Attachments/aa/bb/GUID/copy.png",
        }

        def fake_wait(db_path, chat_id, sent_after_unix_ms, *a, **k):
            self.waits.append({"db_path": db_path, "chat_id": chat_id})
            return dict(self.wait_result)

        bridge.wait_for_attachment_transfer = fake_wait

        # mc-vhnq7: text-only sends now wait on chat.db the same way. Default
        # it to "a bound row showed up"; the failure tests swap in other
        # outcomes. Kept separate from self.waits (the attachment wait) so a
        # test can assert on either without the two being conflated.
        self._orig_wait_text = bridge.wait_for_text_message
        self.text_waits = []
        self.text_wait_result = {
            "outcome": "sent",
            "detail": "message row confirmed",
            "message_rowid": 99,
            "text": None,  # overwritten per-call below with the real text
            "is_sent": 0,
            "error": 0,
        }

        def fake_wait_text(db_path, chat_id, sent_after_unix_ms, expected_text, *a, **k):
            self.text_waits.append(
                {"db_path": db_path, "chat_id": chat_id, "expected_text": expected_text}
            )
            return dict(self.text_wait_result, text=expected_text)

        bridge.wait_for_text_message = fake_wait_text

        # The janitor runs in a thread after a kept send; record instead.
        self._orig_janitor = bridge.start_outbox_janitor
        self.janitor_runs = []
        bridge.start_outbox_janitor = lambda: self.janitor_runs.append(True)

        with bridge._SEND_STATS_LOCK:
            self._orig_stats = dict(bridge.SEND_STATS)
            bridge.SEND_STATS["attachment_sent"] = 0
            bridge.SEND_STATS["attachment_failed"] = 0
            bridge.SEND_STATS["text_confirmed"] = 0
            bridge.SEND_STATS["text_failed"] = 0
            bridge.SEND_STATS["text_unconfirmed"] = 0

        # mc-vhnq7 (idempotency): a clean idempotency map per test, so a key
        # name reused across tests never leaks a stored outcome between them.
        self._orig_idempotency_keys = dict(bridge._IDEMPOTENCY_KEYS)
        bridge._IDEMPOTENCY_KEYS.clear()
        self._orig_idempotency_wait_timeout = bridge.IDEMPOTENCY_WAIT_TIMEOUT_S

        handler = bridge.make_handler(self.db_path, TEST_TOKEN)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        bridge.send_message = self._orig_send
        bridge.wait_for_attachment_transfer = self._orig_wait
        bridge.wait_for_text_message = self._orig_wait_text
        bridge.start_outbox_janitor = self._orig_janitor
        bridge.OUTBOX_DIR = self._orig_outbox
        with bridge._SEND_STATS_LOCK:
            bridge.SEND_STATS.clear()
            bridge.SEND_STATS.update(self._orig_stats)
        bridge._IDEMPOTENCY_KEYS.clear()
        bridge._IDEMPOTENCY_KEYS.update(self._orig_idempotency_keys)
        bridge.IDEMPOTENCY_WAIT_TIMEOUT_S = self._orig_idempotency_wait_timeout

    def _stats(self):
        with bridge._SEND_STATS_LOCK:
            return dict(bridge.SEND_STATS)

    def _b64_payload(self, text="x"):
        return {
            "chat_id": "chat-x",
            "text": text,
            "attachment_b64": base64.b64encode(PNG_BYTES).decode("ascii"),
            "attachment_name": "crop.png",
        }

    def _post(self, payload, token=TEST_TOKEN, idempotency_key=None, timeout=5):
        raw = json.dumps(payload).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers[bridge.AUTH_HEADER] = token
        if idempotency_key is not None:
            headers[bridge.IDEMPOTENCY_HEADER] = idempotency_key
        conn.request("POST", "/send", body=raw, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, json.loads(body)

    def test_text_only_send_is_200_once_chat_db_confirms_it(self):
        # mc-vhnq7: the AppleScript shape is unchanged (see BuildSendScriptTest)
        # but the response now depends on wait_for_text_message, not just
        # osascript's exit code.
        status, body = self._post({"chat_id": "chat-x", "text": "hello"})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "sent")
        self.assertFalse(body["attachment_sent"])
        self.assertEqual(self.sends[0]["attachment_path"], None)
        self.assertEqual(self.text_waits[0]["chat_id"], "chat-x")
        self.assertEqual(self.text_waits[0]["expected_text"], "hello")
        self.assertEqual(self._stats()["text_confirmed"], 1)
        self.assertEqual(self._stats()["text_failed"], 0)

    def test_attachment_path_is_passed_through(self):
        status, body = self._post(
            {"chat_id": "chat-x", "text": "the mark", "attachment_path": self.png}
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["attachment_sent"])
        self.assertEqual(self.sends[0]["attachment_path"], self.png)
        self.assertEqual(self.sends[0]["text"], "the mark")
        # A caller-supplied host path is never deleted by the bridge.
        self.assertTrue(os.path.isfile(self.png))

    def test_attachment_b64_is_staged_then_deleted(self):
        status, body = self._post(
            {
                "chat_id": "chat-x",
                "text": "",
                "attachment_b64": base64.b64encode(PNG_BYTES).decode("ascii"),
                "attachment_name": "crop.png",
            }
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["attachment_sent"])
        staged = self.sends[0]["attachment_path"]
        self.assertTrue(self.sends[0]["attachment_existed_at_send"])
        self.assertFalse(os.path.exists(staged), "staged file outlived the send")
        self.assertEqual(os.listdir(bridge.OUTBOX_DIR), [])

    def test_attachment_alone_with_no_text_field(self):
        status, body = self._post(
            {"chat_id": "chat-x", "attachment_path": self.png}
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.sends[0]["text"], "")

    def test_staged_file_is_deleted_when_the_send_fails(self):
        def failing_send(chat_id, text, attachment_path=None):
            self.sends.append({"attachment_path": attachment_path})
            raise RuntimeError("AppleScript failed (exit 1): boom")

        bridge.send_message = failing_send
        status, body = self._post(
            {
                "chat_id": "chat-x",
                "text": "x",
                "attachment_b64": base64.b64encode(PNG_BYTES).decode("ascii"),
                "attachment_name": "crop.png",
            }
        )
        self.assertEqual(status, 500)
        self.assertFalse(os.path.exists(self.sends[0]["attachment_path"]))

    def test_both_attachment_forms_is_a_400(self):
        status, body = self._post(
            {
                "chat_id": "chat-x",
                "text": "x",
                "attachment_path": self.png,
                "attachment_b64": base64.b64encode(PNG_BYTES).decode("ascii"),
                "attachment_name": "crop.png",
            }
        )
        self.assertEqual(status, 400)
        self.assertIn("not both", body["error"])
        self.assertEqual(self.sends, [])

    def test_missing_text_with_no_attachment_is_still_a_400(self):
        status, body = self._post({"chat_id": "chat-x"})
        self.assertEqual(status, 400)
        self.assertIn("Missing required field: text", body["error"])

    def test_empty_text_with_no_attachment_is_a_400(self):
        status, body = self._post({"chat_id": "chat-x", "text": ""})
        self.assertEqual(status, 400)
        self.assertIn("Nothing to send", body["error"])
        self.assertEqual(self.sends, [])

    def test_relative_attachment_path_is_a_400(self):
        status, body = self._post(
            {"chat_id": "chat-x", "text": "x", "attachment_path": "pic.png"}
        )
        self.assertEqual(status, 400)
        self.assertIn("absolute path", body["error"])
        self.assertEqual(self.sends, [])

    def test_non_image_attachment_path_is_a_400(self):
        doc = os.path.join(self.tmp, "notes.txt")
        with open(doc, "w") as f:
            f.write("text")
        status, body = self._post(
            {"chat_id": "chat-x", "text": "x", "attachment_path": doc}
        )
        self.assertEqual(status, 400)
        self.assertIn("must be an image", body["error"])

    def test_bad_base64_is_a_400(self):
        status, body = self._post(
            {
                "chat_id": "chat-x",
                "text": "x",
                "attachment_b64": "!!! not base64 !!!",
                "attachment_name": "crop.png",
            }
        )
        self.assertEqual(status, 400)
        self.assertIn("not valid base64", body["error"])

    def test_every_pre_send_validation_and_staging_error_is_retry_safe(self):
        # mc-vhnq7 review (Richard, third pass): remote-send.ts now retries a
        # /send error ONLY when the body says "retry_safe": true. Every error
        # that can happen before osascript ever runs - nothing was sent -
        # must carry it, or a caller using that contract would wrongly give
        # up on a request it could safely retry.
        cases = [
            ({"chat_id": "chat-x"}, 400),  # missing text
            ({"chat_id": "chat-x", "text": ""}, 400),  # nothing to send
            ({"chat_id": "chat-x", "text": "x", "attachment_path": "pic.png"}, 400),  # relative path
            (
                {
                    "chat_id": "chat-x", "text": "x",
                    "attachment_b64": "!!! not base64 !!!", "attachment_name": "crop.png",
                },
                400,  # bad base64 (staging)
            ),
        ]
        for payload, expected_status in cases:
            status, body = self._post(payload)
            self.assertEqual(status, expected_status, payload)
            self.assertTrue(body.get("retry_safe"), (payload, body))

    # ---- mc-mnvrm: the response follows chat.db, not osascript's exit ----

    def test_text_only_send_waits_on_the_message_row_not_the_attachment_wait(self):
        # mc-vhnq7 flipped this: a text-only send now DOES wait on chat.db
        # (wait_for_text_message), but it still never touches the attachment
        # confirmation path (wait_for_attachment_transfer) - there is no
        # attachment row to wait for.
        status, _ = self._post({"chat_id": "chat-x", "text": "hello"})
        self.assertEqual(status, 200)
        self.assertEqual(len(self.text_waits), 1)
        self.assertEqual(self.waits, [])

    def test_applescript_ok_but_no_row_appears_is_504_text_unconfirmed(self):
        # The exact ticket scenario (mc-vhnq7): osascript exits 0, but
        # Messages.app never produces a matching chat.db row by the deadline.
        # This is AMBIGUOUS, not a definite failure - Richard's review: the
        # message may still be mid-send, so this must be distinguishable
        # from text_failed and never retried by a caller.
        self.text_wait_result = {
            "outcome": "unconfirmed",
            "detail": "no outgoing chat.db row exactly matching this text appeared within 30s",
        }
        status, body = self._post({"chat_id": "chat-x", "text": "hello"})
        self.assertEqual(status, 504)
        self.assertEqual(body["status"], "text_unconfirmed")
        self.assertEqual(body["text_outcome"], "unconfirmed")
        self.assertFalse(body["retry_safe"])
        self.assertFalse(body["text_sent"])
        self.assertFalse(body["attachment_sent"])
        self.assertIn("not confirmed", body["error"])
        stats = self._stats()
        self.assertEqual(stats["text_unconfirmed"], 1)
        self.assertEqual(stats["text_failed"], 0)
        self.assertEqual(stats["text_confirmed"], 0)
        self.assertIn("text unconfirmed", stats["last_error"])

    def test_chat_db_unreadable_during_the_poll_is_also_504_text_unconfirmed(self):
        # "error" (chat.db unreadable while polling, after osascript already
        # ran) is just as ambiguous as "unconfirmed" - the bridge cannot
        # prove the send failed, so it must not be reported as a definite
        # text_failed either.
        self.text_wait_result = {"outcome": "error", "detail": "chat.db read failed: locked"}
        status, body = self._post({"chat_id": "chat-x", "text": "hello"})
        self.assertEqual(status, 504)
        self.assertEqual(body["status"], "text_unconfirmed")
        self.assertEqual(body["text_outcome"], "error")
        self.assertFalse(body["retry_safe"])
        self.assertEqual(self._stats()["text_unconfirmed"], 1)

    def test_row_with_a_message_error_is_502_text_failed(self):
        # message.error != 0 on a MATCHED row is as strong a failure signal
        # for a text row as message_error already is for an attachment row.
        # This IS a definite failure (unlike "unconfirmed" above): safe to
        # retry, so it keeps the 502/text_failed/retry_safe:true shape.
        self.text_wait_result = {
            "outcome": "failed",
            "detail": "Messages marked the text failed (message error 22)",
            "message_rowid": 101,
            "is_sent": 0,
            "error": 22,
        }
        status, body = self._post({"chat_id": "chat-x", "text": "hello"})
        self.assertEqual(status, 502)
        self.assertEqual(body["status"], "text_failed")
        self.assertEqual(body["text_outcome"], "failed")
        self.assertTrue(body["retry_safe"])
        self.assertIn("message error 22", body["error"])
        self.assertEqual(self._stats()["text_failed"], 1)
        self.assertEqual(self._stats()["text_unconfirmed"], 0)

    def test_text_chat_db_read_error_before_send_is_502_and_nothing_is_sent(self):
        # Unlike the post-send "error" outcome above, THIS error happens
        # before osascript ever runs - nothing was sent, so it is a definite,
        # safe-to-retry failure (502/text_failed), not an ambiguous one.
        orig = bridge.read_message_high_water

        def broken(db_path, chat_id):
            raise sqlite3.OperationalError("unable to open database file")

        bridge.read_message_high_water = broken
        try:
            status, body = self._post({"chat_id": "chat-x", "text": "hello"})
        finally:
            bridge.read_message_high_water = orig
        self.assertEqual(status, 502)
        self.assertEqual(body["status"], "text_failed")
        self.assertEqual(body["text_outcome"], "error")
        self.assertTrue(body["retry_safe"])
        self.assertEqual(self.sends, [], "osascript ran without a baseline")
        self.assertEqual(self.text_waits, [])
        self.assertEqual(self._stats()["text_failed"], 1)

    # ---- osascript itself fails, but may have already submitted the text
    # (mc-vhnq7 review, Richard): a flat 500 here let remote-send.ts retry
    # under its default attempts and risk a double-send. A reported
    # osascript failure now falls through to the SAME chat.db confirmation a
    # clean exit would run, against the SAME pre-send high-water mark. ----

    def test_osascript_timeout_with_the_row_present_is_200_sent(self):
        def timing_out_send(chat_id, text, attachment_path=None):
            self.sends.append({"chat_id": chat_id, "text": text})
            raise subprocess.TimeoutExpired(cmd=["osascript"], timeout=60)

        bridge.send_message = timing_out_send
        # self.text_wait_result defaults to "sent" (see setUp) - the row
        # landed even though osascript itself timed out.
        status, body = self._post({"chat_id": "chat-x", "text": "hello"})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "sent")
        self.assertEqual(len(self.text_waits), 1)
        self.assertEqual(self._stats()["text_confirmed"], 1)

    def test_osascript_timeout_with_no_row_is_504_text_unconfirmed(self):
        def timing_out_send(chat_id, text, attachment_path=None):
            self.sends.append({"chat_id": chat_id, "text": text})
            raise subprocess.TimeoutExpired(cmd=["osascript"], timeout=60)

        bridge.send_message = timing_out_send
        self.text_wait_result = {
            "outcome": "unconfirmed",
            "detail": "no outgoing chat.db row exactly matching this text appeared within 30s",
        }
        status, body = self._post({"chat_id": "chat-x", "text": "hello"})
        self.assertEqual(status, 504)
        self.assertEqual(body["status"], "text_unconfirmed")
        self.assertFalse(body["retry_safe"])
        self.assertEqual(self._stats()["text_unconfirmed"], 1)
        self.assertEqual(self._stats()["text_failed"], 0)

    def test_osascript_nonzero_exit_with_no_row_is_504_text_unconfirmed(self):
        def failing_send(chat_id, text, attachment_path=None):
            self.sends.append({"chat_id": chat_id, "text": text})
            raise RuntimeError("AppleScript failed (exit 1): Messages got an error")

        bridge.send_message = failing_send
        self.text_wait_result = {
            "outcome": "unconfirmed",
            "detail": "no outgoing chat.db row exactly matching this text appeared within 30s",
        }
        status, body = self._post({"chat_id": "chat-x", "text": "hello"})
        self.assertEqual(status, 504)
        self.assertEqual(body["status"], "text_unconfirmed")
        self.assertFalse(body["retry_safe"])
        self.assertEqual(len(self.text_waits), 1, "the confirm wait still ran despite the non-zero exit")
        self.assertEqual(self._stats()["text_unconfirmed"], 1)

    def test_osascript_nonzero_exit_with_a_matched_failed_row_is_502_text_failed(self):
        def failing_send(chat_id, text, attachment_path=None):
            self.sends.append({"chat_id": chat_id, "text": text})
            raise RuntimeError("AppleScript failed (exit 1): Messages got an error")

        bridge.send_message = failing_send
        self.text_wait_result = {
            "outcome": "failed",
            "detail": "Messages marked the text failed (message error 22)",
            "message_rowid": 7,
            "is_sent": 0,
            "error": 22,
        }
        status, body = self._post({"chat_id": "chat-x", "text": "hello"})
        self.assertEqual(status, 502)
        self.assertEqual(body["status"], "text_failed")
        self.assertTrue(body["retry_safe"])
        self.assertEqual(self._stats()["text_failed"], 1)

    def test_osascript_nonzero_exit_on_an_attachment_send_is_still_a_flat_500(self):
        # Scope check: only the text-only path got the confirm-anyway
        # treatment above. An attachment send with no review finding behind
        # it keeps answering 500 immediately, and its staged file is still
        # discarded right away.
        def failing_send(chat_id, text, attachment_path=None):
            self.sends.append({"attachment_path": attachment_path})
            raise RuntimeError("AppleScript failed (exit 1): boom")

        bridge.send_message = failing_send
        status, body = self._post(
            {
                "chat_id": "chat-x",
                "text": "x",
                "attachment_b64": base64.b64encode(PNG_BYTES).decode("ascii"),
                "attachment_name": "crop.png",
            }
        )
        self.assertEqual(status, 500)
        self.assertEqual(self.text_waits, [], "the text confirm wait must not run for an attachment send")
        self.assertFalse(os.path.exists(self.sends[0]["attachment_path"]))

    def test_confirmed_attachment_is_200_with_the_transfer_state(self):
        status, body = self._post(self._b64_payload())
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "sent")
        self.assertTrue(body["attachment_sent"])
        self.assertEqual(body["attachment_transfer_state"], 5)
        self.assertFalse(body["staged_file_kept"])
        self.assertEqual(self.waits[0]["chat_id"], "chat-x")
        self.assertEqual(self.waits[0]["db_path"], self.db_path)
        self.assertEqual(self._stats()["attachment_sent"], 1)
        self.assertEqual(self._stats()["attachment_failed"], 0)

    def test_staged_file_exists_while_the_transfer_is_confirmed(self):
        # The file has to outlive osascript: Messages reads it during the
        # transfer. The wait stub observes the outbox at confirmation time.
        seen = {}

        def fake_wait(db_path, chat_id, sent_after_unix_ms, *a, **k):
            seen["outbox"] = os.listdir(bridge.OUTBOX_DIR)
            return dict(self.wait_result)

        bridge.wait_for_attachment_transfer = fake_wait
        status, _ = self._post(self._b64_payload())
        self.assertEqual(status, 200)
        self.assertEqual(len(seen["outbox"]), 1, "staged file was gone before confirmation")
        self.assertEqual(os.listdir(bridge.OUTBOX_DIR), [], "staged file outlived the send")

    def test_failed_transfer_is_502_never_attachment_sent_true(self):
        self.wait_result = {
            "outcome": "failed",
            "detail": "Messages marked the attachment transfer failed (transfer_state 6, message error 39)",
            "transfer_state": 6,
            "attachment_rowid": 7,
            "filename": "",
        }
        status, body = self._post(self._b64_payload(text="the mark"))
        self.assertEqual(status, 502)
        self.assertEqual(body["status"], "attachment_failed")
        self.assertFalse(body["attachment_sent"])
        self.assertTrue(body["text_sent"])
        self.assertEqual(body["attachment_outcome"], "failed")
        self.assertEqual(body["attachment_transfer_state"], 6)
        self.assertIn("transfer_state 6", body["error"])
        self.assertEqual(os.listdir(bridge.OUTBOX_DIR), [])
        stats = self._stats()
        self.assertEqual(stats["attachment_failed"], 1)
        self.assertEqual(stats["attachment_sent"], 0)
        self.assertIn("attachment failed", stats["last_error"])

    def test_missing_row_is_502(self):
        self.wait_result = {
            "outcome": "missing",
            "detail": "no outgoing attachment row appeared in chat.db within 30s",
        }
        status, body = self._post(self._b64_payload(text=""))
        self.assertEqual(status, 502)
        self.assertEqual(body["attachment_outcome"], "missing")
        self.assertFalse(body["attachment_sent"])
        self.assertFalse(body["text_sent"])
        self.assertIsNone(body["attachment_transfer_state"])
        self.assertEqual(self._stats()["attachment_failed"], 1)

    def test_in_flight_at_timeout_is_502(self):
        self.wait_result = {
            "outcome": "timeout",
            "detail": "attachment still in flight after 30s (transfer_state 1)",
            "transfer_state": 1,
            "attachment_rowid": 9,
            "filename": "",
        }
        status, body = self._post(
            {"chat_id": "chat-x", "text": "t", "attachment_path": self.png}
        )
        self.assertEqual(status, 502)
        self.assertEqual(body["attachment_outcome"], "timeout")
        self.assertEqual(body["attachment_transfer_state"], 1)
        # A caller-supplied host path is never deleted, success or failure.
        self.assertTrue(os.path.isfile(self.png))

    def test_chat_db_read_error_is_502(self):
        self.wait_result = {"outcome": "error", "detail": "chat.db read failed: locked"}
        status, body = self._post(self._b64_payload())
        self.assertEqual(status, 502)
        self.assertEqual(body["attachment_outcome"], "error")
        self.assertEqual(os.listdir(bridge.OUTBOX_DIR), [])

    def test_staged_file_is_kept_when_messages_references_it_directly(self):
        # If the confirmed row's filename IS our staged file, Messages did not
        # copy it; deleting it would orphan the transcript row on the Mac.
        def fake_wait(db_path, chat_id, sent_after_unix_ms, *a, **k):
            staged = os.path.join(bridge.OUTBOX_DIR, os.listdir(bridge.OUTBOX_DIR)[0])
            return dict(self.wait_result, filename=staged)

        bridge.wait_for_attachment_transfer = fake_wait
        status, body = self._post(self._b64_payload())
        self.assertEqual(status, 200)
        self.assertTrue(body["staged_file_kept"])
        self.assertEqual(len(os.listdir(bridge.OUTBOX_DIR)), 1)
        kept = os.path.join(bridge.OUTBOX_DIR, os.listdir(bridge.OUTBOX_DIR)[0])
        self.assertEqual(stat.S_IMODE(os.stat(kept).st_mode), 0o600)
        self.assertEqual(self.janitor_runs, [True], "janitor did not run after a kept file")

    def test_janitor_does_not_run_when_nothing_was_kept(self):
        self._post(self._b64_payload())
        self.wait_result = {"outcome": "missing", "detail": "none"}
        self._post(self._b64_payload())
        self._post({"chat_id": "chat-x", "text": "t", "attachment_path": self.png})
        self.assertEqual(self.janitor_runs, [])

    # ---- request identity and per-chat serialization (Richard's review) ----

    def test_wait_is_bound_to_the_high_water_mark_and_the_sent_file(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO chat (ROWID, guid, style) VALUES (1, 'chat-x', 45)")
        conn.execute("INSERT INTO message (ROWID, date, is_from_me) VALUES (1, 1, 1)")
        conn.execute("INSERT INTO chat_message_join VALUES (1, 1)")
        conn.execute("INSERT INTO attachment (ROWID, filename, transfer_state, is_outgoing) "
                     "VALUES (77, '~/old.png', 5, 1)")
        conn.execute("INSERT INTO message_attachment_join VALUES (1, 77)")
        conn.commit()
        conn.close()
        seen = {}

        def fake_wait(db_path, chat_id, sent_after_unix_ms, *a, **k):
            seen.update(k)
            return dict(self.wait_result)

        bridge.wait_for_attachment_transfer = fake_wait
        status, _ = self._post(self._b64_payload())
        self.assertEqual(status, 200)
        self.assertEqual(seen["after_rowid"], 77)
        self.assertEqual(seen["expected_path"], self.sends[0]["attachment_path"])

    def test_unreadable_chat_db_before_the_send_is_502_and_nothing_is_sent(self):
        orig = bridge.read_attachment_high_water

        def broken(db_path, chat_id):
            raise sqlite3.OperationalError("unable to open database file")

        bridge.read_attachment_high_water = broken
        try:
            status, body = self._post(self._b64_payload(text="the mark"))
        finally:
            bridge.read_attachment_high_water = orig
        self.assertEqual(status, 502)
        self.assertEqual(body["attachment_outcome"], "error")
        self.assertFalse(body["text_sent"])
        self.assertFalse(body["attachment_sent"])
        self.assertTrue(body["retry_safe"])
        self.assertEqual(self.sends, [], "osascript ran without a baseline")
        self.assertEqual(self.waits, [])
        self.assertEqual(os.listdir(bridge.OUTBOX_DIR), [])
        self.assertEqual(self._stats()["attachment_failed"], 1)

    def test_overlapping_same_chat_sends_each_get_their_own_outcome(self):
        # Regression 2. Two POSTs to the same chat at once, through the REAL
        # wait against the test chat.db. The stand-in for osascript inserts
        # the attachment row Messages would create: "good.png" ends at
        # transfer_state 5, "bad.png" at 6. Each response must report its own
        # row, and the two sends must not overlap (per-chat lock).
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO chat (ROWID, guid, style) VALUES (1, 'chat-x', 45)")
        conn.commit()
        conn.close()
        bridge.wait_for_attachment_transfer = self._orig_wait
        intervals = []
        lock = threading.Lock()

        def db_send(chat_id, text, attachment_path=None):
            start = time.monotonic()
            time.sleep(0.15)  # long enough for the other request to arrive
            state = 5 if attachment_path.endswith("-good.png") else 6
            c = sqlite3.connect(self.db_path)
            cur = c.cursor()
            cur.execute(
                "INSERT INTO message (date, is_from_me) VALUES (?, 1)",
                (bridge.unix_ms_to_apple_ns(int(time.time() * 1000)),),
            )
            msg = cur.lastrowid
            cur.execute("INSERT INTO chat_message_join VALUES (1, ?)", (msg,))
            cur.execute(
                "INSERT INTO attachment (filename, transfer_state, is_outgoing) VALUES (?, ?, 1)",
                (attachment_path, state),
            )
            cur.execute("INSERT INTO message_attachment_join VALUES (?, ?)", (msg, cur.lastrowid))
            c.commit()
            c.close()
            with lock:
                self.sends.append({"attachment_path": attachment_path})
                intervals.append((start, time.monotonic()))
            return 0.01

        bridge.send_message = db_send
        results = {}

        def post(name):
            payload = self._b64_payload(text="")
            payload["attachment_name"] = name
            results[name] = self._post(payload)

        threads = [threading.Thread(target=post, args=(n,)) for n in ("good.png", "bad.png")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        good_status, good = results["good.png"]
        bad_status, bad = results["bad.png"]
        self.assertEqual(good_status, 200)
        self.assertTrue(good["attachment_sent"])
        self.assertEqual(good["attachment_transfer_state"], 5)
        self.assertEqual(bad_status, 502)
        self.assertEqual(bad["attachment_outcome"], "failed")
        self.assertEqual(bad["attachment_transfer_state"], 6)
        # Each response named its own row: the good row is 5, the bad row is 6,
        # and the bridge did not hand the good outcome to both.
        stats = self._stats()
        self.assertEqual(stats["attachment_sent"], 1)
        self.assertEqual(stats["attachment_failed"], 1)
        # The lock held: the second send began after the first one ended.
        self.assertEqual(len(intervals), 2)
        (s1, e1), (s2, e2) = sorted(intervals)
        self.assertGreaterEqual(s2, e1, "sends to the same chat overlapped")

    def test_the_chat_lock_is_held_through_the_confirmation_wait(self):
        # Richard's review at d4b1d13 (P3): the overlap test above records
        # intervals that end when send_message returns, so it proves only that
        # the two osascript calls do not overlap. This test parks the FIRST
        # request inside wait_for_attachment_transfer and proves the second
        # same-chat request cannot read its high-water mark or run its send
        # until the first confirmation releases. Moving the wait outside the
        # lock makes this fail: the second mark and send land while the first
        # request is still parked.
        parked = threading.Event()
        release = threading.Event()
        events = []
        elock = threading.Lock()

        def record(name):
            with elock:
                events.append(name)

        def which(path):
            return "first" if path.endswith("-first.png") else "second"

        orig_high_water = bridge.read_attachment_high_water

        def recording_high_water(db_path, chat_id):
            record("mark")
            return orig_high_water(db_path, chat_id)

        def recording_send(chat_id, text, attachment_path=None):
            record("send:" + which(attachment_path))
            return 0.01

        def parking_wait(db_path, chat_id, sent_after_unix_ms, *a, **k):
            name = which(k["expected_path"])
            record("wait:" + name)
            if name == "first":
                parked.set()
                self.assertTrue(release.wait(10), "test released nothing")
            return dict(self.wait_result)

        bridge.read_attachment_high_water = recording_high_water
        bridge.send_message = recording_send
        bridge.wait_for_attachment_transfer = parking_wait
        results = {}

        def post(name):
            payload = self._b64_payload(text="")
            payload["attachment_name"] = name + ".png"
            results[name] = self._post(payload)

        try:
            first = threading.Thread(target=post, args=("first",))
            first.start()
            self.assertTrue(parked.wait(5), "first request never reached its wait")
            second = threading.Thread(target=post, args=("second",))
            second.start()
            # The second request stages its file BEFORE taking the chat lock,
            # so two staged files prove it has arrived and is inside the route.
            # If the lock is not held through the wait, the second request
            # runs to completion instead (its mark read and send land in
            # events), so the poll also stops on that.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with elock:
                    leaked = len(events) > 3
                if leaked or len(os.listdir(bridge.OUTBOX_DIR)) >= 2:
                    break
                time.sleep(0.02)
            time.sleep(0.3)  # grace: if the lock were released, the mark read would land here
            with elock:
                self.assertEqual(
                    events, ["mark", "send:first", "wait:first"],
                    "second request entered its mark read or send while the first "
                    "confirmation was still running",
                )
            self.assertEqual(len(os.listdir(bridge.OUTBOX_DIR)), 2, "second request never staged")
            release.set()
            first.join(5)
            second.join(5)
        finally:
            release.set()
            bridge.read_attachment_high_water = orig_high_water

        self.assertEqual(results["first"][0], 200)
        self.assertEqual(results["second"][0], 200)
        self.assertEqual(
            events,
            ["mark", "send:first", "wait:first", "mark", "send:second", "wait:second"],
        )

    def test_sends_to_different_chats_are_not_serialized(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        def slow_send(chat_id, text, attachment_path=None):
            calls.append(chat_id)
            if chat_id == "chat-a":
                started.set()
                self.assertTrue(release.wait(5))
            return 0.01

        bridge.send_message = slow_send
        a = threading.Thread(
            target=self._post,
            args=({"chat_id": "chat-a", "text": "", "attachment_path": self.png},),
        )
        a.start()
        self.assertTrue(started.wait(5))
        status, _ = self._post({"chat_id": "chat-b", "text": "", "attachment_path": self.png})
        self.assertEqual(status, 200)
        self.assertEqual(calls, ["chat-a", "chat-b"])
        release.set()
        a.join(5)

    # ---- idempotency key (mc-vhnq7, Richard's review) ----

    def test_no_key_means_no_deduplication(self):
        # "A request without a key behaves exactly as now."
        status1, _ = self._post({"chat_id": "chat-x", "text": "hello"})
        status2, _ = self._post({"chat_id": "chat-x", "text": "hello"})
        self.assertEqual(status1, 200)
        self.assertEqual(status2, 200)
        self.assertEqual(len(self.sends), 2, "without a key each request runs its own osascript call")

    def test_same_key_twice_sequentially_runs_osascript_once_with_an_identical_response(self):
        status1, body1 = self._post({"chat_id": "chat-x", "text": "hello"}, idempotency_key="k-repeat")
        status2, body2 = self._post({"chat_id": "chat-x", "text": "hello"}, idempotency_key="k-repeat")
        self.assertEqual(status1, 200)
        self.assertEqual(status1, status2)
        self.assertEqual(body1, body2)
        self.assertEqual(len(self.sends), 1, "a repeat key must not run osascript again")
        self.assertEqual(self._stats()["text_confirmed"], 1, "the replay must not double-count the outcome")

    def test_repeat_key_replays_the_exact_504_including_its_body(self):
        # "The stored outcome keeps the exact status and body, including a
        # 504 marked ambiguous."
        self.text_wait_result = {
            "outcome": "unconfirmed",
            "detail": "no outgoing chat.db row exactly matching this text appeared within 30s",
        }
        status1, body1 = self._post({"chat_id": "chat-x", "text": "hello"}, idempotency_key="k-504")
        status2, body2 = self._post({"chat_id": "chat-x", "text": "hello"}, idempotency_key="k-504")
        self.assertEqual(status1, 504)
        self.assertEqual(status1, status2)
        self.assertEqual(body1, body2)
        self.assertFalse(body1["retry_safe"])
        self.assertEqual(len(self.sends), 1, "the replay must not run osascript again")

    def test_key_reuse_with_different_text_is_409_and_not_sent(self):
        status1, _ = self._post({"chat_id": "chat-x", "text": "hello"}, idempotency_key="k-conflict-text")
        self.assertEqual(status1, 200)
        status2, body2 = self._post(
            {"chat_id": "chat-x", "text": "something else"}, idempotency_key="k-conflict-text"
        )
        self.assertEqual(status2, 409)
        self.assertEqual(body2["status"], "idempotency_conflict")
        self.assertNotIn("retry_safe", body2, "a 409 must never be marked retry_safe - it is never retried")
        self.assertEqual(len(self.sends), 1, "the conflicting request must never reach osascript")

    def test_key_reuse_with_different_chat_is_409_and_not_sent(self):
        status1, _ = self._post({"chat_id": "chat-x", "text": "hello"}, idempotency_key="k-conflict-chat")
        self.assertEqual(status1, 200)
        status2, body2 = self._post(
            {"chat_id": "chat-y", "text": "hello"}, idempotency_key="k-conflict-chat"
        )
        self.assertEqual(status2, 409)
        self.assertEqual(len(self.sends), 1)

    def test_key_reuse_with_a_different_attachment_is_409_and_not_sent(self):
        # The key binds to attachment identity too, not just chat and text
        # (Richard's review): a different image under the same key, chat,
        # and text is still a conflict.
        status1, _ = self._post(self._b64_payload(text="hello"), idempotency_key="k-conflict-attachment")
        self.assertEqual(status1, 200)
        other_payload = {
            "chat_id": "chat-x",
            "text": "hello",
            "attachment_b64": base64.b64encode(PNG_BYTES + b"\x00").decode("ascii"),
            "attachment_name": "crop.png",
        }
        status2, body2 = self._post(other_payload, idempotency_key="k-conflict-attachment")
        self.assertEqual(status2, 409)
        self.assertEqual(len(self.sends), 1)

    def test_same_path_with_changed_bytes_under_the_same_key_is_409_not_a_replay(self):
        # Richard's review: attachment_path identity used to be the path
        # STRING only, so overwriting the file between two requests that
        # reuse the same key and path still compared equal, and the second
        # request replayed the first one's (now-stale) outcome. Identity is
        # now the file's content hash, so changed bytes under the same path
        # and key must come back a conflict, never a replay and never a
        # second osascript call for the new content either.
        status1, _ = self._post(
            {"chat_id": "chat-x", "text": "x", "attachment_path": self.png},
            idempotency_key="k-path-bytes-changed",
        )
        self.assertEqual(status1, 200)
        with open(self.png, "wb") as f:
            f.write(PNG_BYTES + b"\x00\x00\x00\x00")  # same path, different content
        status2, body2 = self._post(
            {"chat_id": "chat-x", "text": "x", "attachment_path": self.png},
            idempotency_key="k-path-bytes-changed",
        )
        self.assertEqual(status2, 409)
        self.assertEqual(body2["status"], "idempotency_conflict")
        self.assertEqual(len(self.sends), 1, "the changed-content request must never reach osascript")

    def test_retry_safe_pre_send_failure_is_not_cached_a_same_key_retry_then_sends(self):
        # Blocker (Richard's review): a 502 retry_safe:true (nothing sent)
        # used to be cached under the key like any other outcome, so a
        # same-key retry just replayed the SAME 502 forever instead of
        # actually trying again. The first attempt here fails before
        # osascript runs (chat.db unreadable for the high-water read); the
        # retry, with the SAME key, must become a fresh owner and actually
        # send.
        orig = bridge.read_message_high_water
        calls = {"n": 0}

        def flaky(db_path, chat_id):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return orig(db_path, chat_id)

        bridge.read_message_high_water = flaky
        try:
            status1, body1 = self._post(
                {"chat_id": "chat-x", "text": "hello"}, idempotency_key="k-retry-safe-not-cached",
            )
            self.assertEqual(status1, 502)
            self.assertEqual(body1["status"], "text_failed")
            self.assertTrue(body1["retry_safe"])
            self.assertEqual(self.sends, [], "the failed first attempt must never reach osascript")

            status2, body2 = self._post(
                {"chat_id": "chat-x", "text": "hello"}, idempotency_key="k-retry-safe-not-cached",
            )
            self.assertEqual(status2, 200)
            self.assertEqual(body2["status"], "sent")
        finally:
            bridge.read_message_high_water = orig

        self.assertEqual(len(self.sends), 1, "osascript must run exactly once - on the successful retry")

    def test_concurrent_requests_with_the_same_key_share_one_outcome(self):
        # "A repeat key whose first request is still in flight waits for
        # that outcome... and returns it." Exercised through a REAL second
        # thread hitting the REAL HTTP server, not a mocked wait.
        release = threading.Event()
        calls = []

        def slow_wait_text(db_path, chat_id, sent_after_unix_ms, expected_text, *a, **k):
            calls.append(1)
            release.wait(timeout=10)
            return dict(self.text_wait_result, text=expected_text)

        bridge.wait_for_text_message = slow_wait_text
        results = {}

        def post(name):
            results[name] = self._post(
                {"chat_id": "chat-x", "text": "hello"}, idempotency_key="k-concurrent", timeout=15,
            )

        first = threading.Thread(target=post, args=("first",))
        first.start()
        self.assertTrue(
            self._wait_until(lambda: len(calls) == 1, timeout=5),
            "the owner must have reached the confirm wait before the second request starts",
        )
        second = threading.Thread(target=post, args=("second",))
        second.start()
        time.sleep(0.2)  # let the second request register as "wait" and block on idempotency_await
        release.set()
        first.join(timeout=10)
        second.join(timeout=10)

        status1, body1 = results["first"]
        status2, body2 = results["second"]
        self.assertEqual(status1, 200)
        self.assertEqual(status1, status2)
        self.assertEqual(body1, body2)
        self.assertEqual(len(self.sends), 1, "osascript must run exactly once for two concurrent same-key requests")
        self.assertEqual(len(calls), 1, "only the owner calls wait_for_text_message; a waiter never starts its own")

    def test_a_waiter_that_times_out_never_sends_and_answers_504(self):
        # "A waiter must never start a second osascript, even after the
        # first request times out. It returns the first request's outcome,
        # or 504 if that is still unknown" (Richard's review). The owner's
        # own confirm wait is held open past this waiter's own (shortened)
        # patience, so the waiter must give up with 504 rather than send.
        bridge.IDEMPOTENCY_WAIT_TIMEOUT_S = 0.2
        release = threading.Event()
        calls = []

        def slow_wait_text(db_path, chat_id, sent_after_unix_ms, expected_text, *a, **k):
            calls.append(1)
            release.wait(timeout=10)
            return dict(self.text_wait_result, text=expected_text)

        bridge.wait_for_text_message = slow_wait_text
        results = {}

        def post(name):
            results[name] = self._post(
                {"chat_id": "chat-x", "text": "hello"}, idempotency_key="k-waiter-timeout", timeout=15,
            )

        owner = threading.Thread(target=post, args=("owner",))
        owner.start()
        self.assertTrue(
            self._wait_until(lambda: len(calls) == 1, timeout=5),
            "the owner must have reached the confirm wait before the waiter's own wait begins",
        )
        status2, body2 = self._post(
            {"chat_id": "chat-x", "text": "hello"}, idempotency_key="k-waiter-timeout", timeout=15,
        )
        self.assertEqual(status2, 504)
        self.assertEqual(body2["status"], "text_unconfirmed")
        self.assertFalse(body2["retry_safe"])

        release.set()
        owner.join(timeout=10)
        self.assertEqual(len(self.sends), 1, "the waiter giving up must never cause its own osascript call")
        self.assertEqual(len(calls), 1, "only the owner ever calls wait_for_text_message")

    def _wait_until(self, predicate, timeout=5, poll=0.02):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(poll)
        return predicate()

    def test_healthz_exposes_the_attachment_counters(self):
        self._post(self._b64_payload())
        self.wait_result = {"outcome": "missing", "detail": "none"}
        self._post(self._b64_payload())
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/healthz", headers={bridge.AUTH_HEADER: TEST_TOKEN})
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        self.assertEqual(body["send_stats"]["attachment_sent"], 1)
        self.assertEqual(body["send_stats"]["attachment_failed"], 1)


def _apple_ns(unix_ms):
    return bridge.unix_ms_to_apple_ns(unix_ms)


class WaitForAttachmentTransferTest(unittest.TestCase):
    """wait_for_attachment_transfer against a real sqlite file shaped like
    chat.db. Time is injected so no test sleeps."""

    CHAT = "iMessage;-;self@example.invalid"
    SENT_AFTER_MS = 1_700_000_000_000

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mc-mnvrm-")
        self.db_path = os.path.join(self.tmp, "chat.db")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.executescript(MINIMAL_SCHEMA)
        self.conn.execute("INSERT INTO chat (ROWID, guid, style) VALUES (1, ?, 45)", (self.CHAT,))
        self.conn.execute("INSERT INTO chat (ROWID, guid, style) VALUES (2, 'iMessage;-;other', 45)")
        self.conn.commit()
        self.clock = [0.0]
        self.slept = []

    def tearDown(self):
        self.conn.close()

    def _now(self):
        return self.clock[0]

    def _sleep(self, s):
        self.slept.append(s)
        self.clock[0] += s

    def _wait(self, timeout_s=10.0):
        return bridge.wait_for_attachment_transfer(
            self.db_path, self.CHAT, self.SENT_AFTER_MS,
            timeout_s=timeout_s, poll_s=0.5, now=self._now, sleep=self._sleep,
        )

    def _add_row(self, chat_rowid=1, offset_ms=1000, transfer_state=0,
                 is_outgoing=1, is_from_me=1, filename="~/x.png", error=0,
                 msg_rowid=None, att_rowid=None):
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO message (ROWID, guid, text, date, is_from_me, is_sent, error) "
            "VALUES (?, ?, '', ?, ?, 0, ?)",
            (msg_rowid, "m-%s" % (msg_rowid or "x"), _apple_ns(self.SENT_AFTER_MS + offset_ms), is_from_me, error),
        )
        msg_rowid = cur.lastrowid
        cur.execute("INSERT INTO chat_message_join VALUES (?, ?)", (chat_rowid, msg_rowid))
        cur.execute(
            "INSERT INTO attachment (ROWID, guid, filename, transfer_state, is_outgoing) "
            "VALUES (?, ?, ?, ?, ?)",
            (att_rowid, "a-%s" % (att_rowid or "x"), filename, transfer_state, is_outgoing),
        )
        att_rowid = cur.lastrowid
        cur.execute("INSERT INTO message_attachment_join VALUES (?, ?)", (msg_rowid, att_rowid))
        self.conn.commit()
        return att_rowid

    def _set_state(self, att_rowid, state):
        self.conn.execute("UPDATE attachment SET transfer_state = ? WHERE ROWID = ?", (state, att_rowid))
        self.conn.commit()

    def test_done_row_is_sent(self):
        rowid = self._add_row(transfer_state=5, filename="~/Library/Messages/Attachments/a/b/c.png")
        result = self._wait()
        self.assertEqual(result["outcome"], "sent")
        self.assertEqual(result["attachment_rowid"], rowid)
        self.assertEqual(result["transfer_state"], 5)
        self.assertEqual(result["filename"], "~/Library/Messages/Attachments/a/b/c.png")
        self.assertEqual(self.slept, [])

    def test_failed_row_is_failed_with_the_message_error(self):
        self._add_row(transfer_state=6, error=39)
        result = self._wait()
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("transfer_state 6", result["detail"])
        self.assertIn("error 39", result["detail"])

    def test_polls_until_the_state_becomes_final(self):
        rowid = self._add_row(transfer_state=0)
        original_sleep = self._sleep

        def sleep_then_finish(s):
            original_sleep(s)
            if len(self.slept) == 3:
                self._set_state(rowid, 5)

        result = bridge.wait_for_attachment_transfer(
            self.db_path, self.CHAT, self.SENT_AFTER_MS,
            timeout_s=10.0, poll_s=0.5, now=self._now, sleep=sleep_then_finish,
        )
        self.assertEqual(result["outcome"], "sent")
        self.assertEqual(len(self.slept), 3)

    def test_row_still_in_flight_at_the_deadline_is_timeout(self):
        self._add_row(transfer_state=1)
        result = self._wait(timeout_s=2.0)
        self.assertEqual(result["outcome"], "timeout")
        self.assertEqual(result["transfer_state"], 1)
        self.assertIn("2s", result["detail"])
        self.assertGreaterEqual(self.clock[0], 2.0)

    def test_no_row_at_all_is_missing(self):
        result = self._wait(timeout_s=1.0)
        self.assertEqual(result["outcome"], "missing")
        self.assertNotIn("transfer_state", result)

    def test_ignores_rows_older_than_the_send(self):
        self._add_row(offset_ms=-5000, transfer_state=5)
        result = self._wait(timeout_s=1.0)
        self.assertEqual(result["outcome"], "missing")

    def test_ignores_other_chats_incoming_and_inbound_attachments(self):
        self._add_row(chat_rowid=2, transfer_state=5)
        self._add_row(is_from_me=0, transfer_state=5)
        self._add_row(is_outgoing=0, transfer_state=5)
        result = self._wait(timeout_s=1.0)
        self.assertEqual(result["outcome"], "missing")

    # ---- request identity (Richard's review at 104ba51): the row must be
    # THIS send's row, not the newest row in the chat ----

    def _staged(self, name="crop.png"):
        path = os.path.join(self.tmp, "%s-%s" % ("c" * 32, name))
        with open(path, "wb") as f:
            f.write(PNG_BYTES)
        return path

    def _wait_bound(self, after_rowid, expected_path, timeout_s=10.0, sleep=None):
        return bridge.wait_for_attachment_transfer(
            self.db_path, self.CHAT, self.SENT_AFTER_MS,
            timeout_s=timeout_s, poll_s=0.5, now=self._now,
            sleep=sleep or self._sleep,
            after_rowid=after_rowid, expected_path=expected_path,
        )

    def test_high_water_mark_reads_the_chats_highest_outgoing_attachment(self):
        self.assertEqual(bridge.read_attachment_high_water(self.db_path, self.CHAT), 0)
        self._add_row(transfer_state=5, att_rowid=10)
        self._add_row(chat_rowid=2, transfer_state=5, att_rowid=50)   # other chat
        self._add_row(is_outgoing=0, transfer_state=5, att_rowid=60)  # inbound
        self.assertEqual(bridge.read_attachment_high_water(self.db_path, self.CHAT), 10)
        with self.assertRaises(sqlite3.Error):
            bridge.read_attachment_high_water(os.path.join(self.tmp, "nope.db"), self.CHAT)

    def test_a_completed_row_inside_the_slack_window_is_not_this_sends_row(self):
        # Regression 1. A picture delivered one second BEFORE this send has a
        # date inside the two-second slack, so the date filter admits it. It
        # sits at the high-water mark, so it is excluded: the outcome stays
        # "missing" until this send's own row appears.
        staged = self._staged()
        prior = self._add_row(offset_ms=1000, transfer_state=5, att_rowid=10,
                              filename="~/Library/Messages/Attachments/a/b/earlier.png")
        mark = bridge.read_attachment_high_water(self.db_path, self.CHAT)
        self.assertEqual(mark, prior)

        result = self._wait_bound(mark, staged, timeout_s=1.0)
        self.assertEqual(result["outcome"], "missing")
        self.assertNotIn("attachment_rowid", result)

        # Same setup, but this send's row lands during the third poll.
        self.clock[0] = 0.0
        self.slept = []
        original_sleep = self._sleep

        def sleep_then_land(s):
            original_sleep(s)
            if len(self.slept) == 3:
                self._add_row(offset_ms=2500, transfer_state=5, att_rowid=11, filename=staged)

        result = self._wait_bound(mark, staged, sleep=sleep_then_land)
        self.assertEqual(result["outcome"], "sent")
        self.assertEqual(result["attachment_rowid"], 11)
        self.assertEqual(result["filename"], staged)

    def test_the_newest_row_does_not_win_when_it_is_not_this_sends_file(self):
        # Replaces test_newest_row_wins, which codified the unsafe selector.
        # Rows 10 and 11 are newer than the mark but belong to other files; 12
        # is ours and still in flight. The others' final states are not ours.
        staged = self._staged()
        mark = 5
        self._add_row(transfer_state=6, att_rowid=10, filename="~/other-a.png")
        self._add_row(transfer_state=5, att_rowid=11, offset_ms=2000, filename="~/other-b.png")
        ours = self._add_row(transfer_state=0, att_rowid=12, offset_ms=1500, filename=staged)

        result = self._wait_bound(mark, staged, timeout_s=1.0)
        self.assertEqual(result["outcome"], "timeout")
        self.assertEqual(result["attachment_rowid"], ours)

        self._set_state(ours, 6)
        result = self._wait_bound(mark, staged)
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["attachment_rowid"], ours)

        # And the inverse: ours is the oldest of the new rows and it delivered,
        # while a newer row for another file failed.
        self._set_state(ours, 5)
        self._add_row(transfer_state=6, att_rowid=13, offset_ms=3000, filename="~/other-c.png")
        result = self._wait_bound(mark, staged)
        self.assertEqual(result["outcome"], "sent")
        self.assertEqual(result["attachment_rowid"], ours)

    def test_rows_at_or_below_the_mark_never_count_even_with_our_path(self):
        staged = self._staged()
        self._add_row(transfer_state=5, att_rowid=10, filename=staged)
        result = self._wait_bound(10, staged, timeout_s=1.0)
        self.assertEqual(result["outcome"], "missing")

    def test_an_unnamed_final_row_above_the_mark_is_not_this_sends_row(self):
        # Richard's review at d4b1d13 (P2), his reproduction: mark 10, a new
        # same-chat outgoing row 11 with filename NULL already at state 5,
        # expected path present. The lock serializes this process only; that
        # row can be a manual Messages send or a half-joined earlier row. It
        # must not authorize "sent" (or, at state 6, "failed") for this
        # request. The outcome stays "missing" until the row gains our name.
        staged = self._staged()
        self._add_row(transfer_state=5, att_rowid=10, filename="~/earlier.png")
        unnamed = self._add_row(transfer_state=5, att_rowid=11, filename=None)
        self.assertEqual(bridge.read_attachment_high_water(self.db_path, self.CHAT), 11)

        records = []
        handler = logging.Handler()
        handler.emit = records.append
        logger = logging.getLogger("bridge")
        old_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            result = self._wait_bound(10, staged, timeout_s=1.0)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
        self.assertEqual(result["outcome"], "missing", "an unnamed delivered row was reported as ours")
        self.assertNotIn("attachment_rowid", result)
        # Observed for diagnostics, once per row across the polls, not per poll.
        noted = [r.getMessage() for r in records if "row 11" in r.getMessage() and "no filename" in r.getMessage()]
        self.assertEqual(len(noted), 1, [r.getMessage() for r in records])
        self.assertGreater(len(self.slept), 1)

        self._set_state(unnamed, 6)
        result = self._wait_bound(10, staged, timeout_s=1.0)
        self.assertEqual(result["outcome"], "missing", "an unnamed failed row was reported as ours")

        # An unnamed row plus a named-for-another-file row: still not ours.
        self._add_row(transfer_state=5, att_rowid=12, offset_ms=2000, filename="~/other.png")
        result = self._wait_bound(10, staged, timeout_s=1.0)
        self.assertEqual(result["outcome"], "missing")

        # The row gains the expected filename: now, and only now, it is ours.
        self.conn.execute(
            "UPDATE attachment SET filename = ?, transfer_state = 5 WHERE ROWID = ?",
            (staged, unnamed),
        )
        self.conn.commit()
        result = self._wait_bound(10, staged)
        self.assertEqual(result["outcome"], "sent")
        self.assertEqual(result["attachment_rowid"], unnamed)

    def test_the_selector_never_returns_an_unnamed_row_when_a_path_is_expected(self):
        # Direct check of the one-shot query at the production shape, both
        # final states, so the rule does not depend on the wait loop.
        staged = self._staged()
        for state in (5, 6):
            rowid = self._add_row(transfer_state=state, filename=None)
            self.assertIsNone(
                bridge._query_outgoing_attachment_for_send(
                    self.db_path, self.CHAT, _apple_ns(self.SENT_AFTER_MS), 0, staged
                ),
                "unnamed row %s at state %s was selected" % (rowid, state),
            )
        # Without an expected path the high-water rule alone still applies.
        found = bridge._query_outgoing_attachment_for_send(
            self.db_path, self.CHAT, _apple_ns(self.SENT_AFTER_MS), 0, None
        )
        self.assertIsNotNone(found)

    def test_binds_by_basename_when_messages_copied_the_file(self):
        staged = self._staged("crop.png")
        copied = "~/Library/Messages/Attachments/1f/15/GUID/" + os.path.basename(staged)
        self._add_row(transfer_state=5, att_rowid=10, filename="~/other.png")
        ours = self._add_row(transfer_state=5, att_rowid=11, filename=copied)
        result = self._wait_bound(5, staged)
        self.assertEqual(result["outcome"], "sent")
        self.assertEqual(result["attachment_rowid"], ours)

    def test_without_a_path_the_identity_is_the_lowest_row_above_the_mark(self):
        self._add_row(transfer_state=5, att_rowid=10)
        first_new = self._add_row(transfer_state=6, att_rowid=11)
        self._add_row(transfer_state=5, att_rowid=12, offset_ms=2000)
        result = self._wait_bound(10, None)
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["attachment_rowid"], first_new)

    def test_unreadable_db_is_error(self):
        result = bridge.wait_for_attachment_transfer(
            os.path.join(self.tmp, "nope.db"), self.CHAT, self.SENT_AFTER_MS,
            timeout_s=1.0, poll_s=0.5, now=self._now, sleep=self._sleep,
        )
        self.assertEqual(result["outcome"], "error")

    def test_kept_file_detection_expands_tilde_and_realpath(self):
        staged = os.path.join(self.tmp, "staged.png")
        with open(staged, "wb") as f:
            f.write(PNG_BYTES)
        self.assertTrue(bridge._messages_kept_the_staged_file({"filename": staged}, staged))
        self.assertFalse(bridge._messages_kept_the_staged_file({"filename": "~/other.png"}, staged))
        self.assertFalse(bridge._messages_kept_the_staged_file({"filename": ""}, staged))
        self.assertFalse(bridge._messages_kept_the_staged_file({"outcome": "missing"}, staged))
        self.assertFalse(bridge._messages_kept_the_staged_file({"filename": staged}, None))


class WaitForTextMessageTest(unittest.TestCase):
    """wait_for_text_message against a real sqlite file shaped like chat.db
    (mc-vhnq7). Time is injected so no test sleeps."""

    CHAT = "iMessage;-;self@example.invalid"
    SENT_AFTER_MS = 1_700_000_000_000

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mc-vhnq7-")
        self.db_path = os.path.join(self.tmp, "chat.db")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.executescript(MINIMAL_SCHEMA)
        self.conn.execute("INSERT INTO chat (ROWID, guid, style) VALUES (1, ?, 45)", (self.CHAT,))
        self.conn.execute("INSERT INTO chat (ROWID, guid, style) VALUES (2, 'iMessage;-;other', 45)")
        self.conn.commit()
        self.clock = [0.0]
        self.slept = []

    def tearDown(self):
        self.conn.close()

    def _now(self):
        return self.clock[0]

    def _sleep(self, s):
        self.slept.append(s)
        self.clock[0] += s

    def _wait(self, expected_text="hello", timeout_s=2.0, after_rowid=None):
        return bridge.wait_for_text_message(
            self.db_path, self.CHAT, self.SENT_AFTER_MS, expected_text,
            timeout_s=timeout_s, poll_s=0.5, now=self._now, sleep=self._sleep,
            after_rowid=after_rowid,
        )

    def _add_row(self, chat_rowid=1, offset_ms=1000, is_from_me=1, text="hello",
                 error=0, msg_rowid=None, attributed_body=None):
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO message (ROWID, guid, text, attributedBody, date, "
            "is_from_me, is_sent, error) VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
            (msg_rowid, "m-%s" % (msg_rowid or "x"), text, attributed_body,
             _apple_ns(self.SENT_AFTER_MS + offset_ms), is_from_me, error),
        )
        msg_rowid = cur.lastrowid
        cur.execute("INSERT INTO chat_message_join VALUES (?, ?)", (chat_rowid, msg_rowid))
        self.conn.commit()
        return msg_rowid

    # ---- the ticket scenario: osascript exits 0, row never shows up ----

    def test_no_row_at_all_is_unconfirmed(self):
        result = self._wait(timeout_s=1.0)
        self.assertEqual(result["outcome"], "unconfirmed")
        self.assertNotIn("message_rowid", result)

    def test_ignores_rows_older_than_the_send(self):
        self._add_row(offset_ms=-5000)
        result = self._wait(timeout_s=1.0)
        self.assertEqual(result["outcome"], "unconfirmed")

    def test_ignores_other_chats_and_inbound_messages(self):
        self._add_row(chat_rowid=2)
        self._add_row(is_from_me=0)
        result = self._wait(timeout_s=1.0)
        self.assertEqual(result["outcome"], "unconfirmed")

    def test_rows_at_or_below_the_mark_never_count(self):
        rowid = self._add_row()
        result = self._wait(after_rowid=rowid, timeout_s=1.0)
        self.assertEqual(result["outcome"], "unconfirmed")

    # ---- a row appears: 200 ----

    def test_matching_row_is_sent(self):
        rowid = self._add_row(text="hello")
        result = self._wait()
        self.assertEqual(result["outcome"], "sent")
        self.assertEqual(result["message_rowid"], rowid)
        self.assertEqual(self.slept, [])

    def test_polls_until_the_row_appears(self):
        original_sleep = self._sleep
        added = {}

        def sleep_then_add(s):
            original_sleep(s)
            if len(self.slept) == 3:
                added["rowid"] = self._add_row(text="hello")

        result = bridge.wait_for_text_message(
            self.db_path, self.CHAT, self.SENT_AFTER_MS, "hello",
            timeout_s=10.0, poll_s=0.5, now=self._now, sleep=sleep_then_add,
        )
        self.assertEqual(result["outcome"], "sent")
        self.assertEqual(result["message_rowid"], added["rowid"])
        self.assertEqual(len(self.slept), 3)

    # ---- Richard's review (mc-vhnq7): exact match only, never "newest" ----

    def test_a_row_with_different_text_is_ignored_not_claimed(self):
        # The earlier (rejected) design bound to "the newest candidate" when
        # nothing matched. A manual Messages send, or another process writing
        # to the same chat during the wait, lands exactly this shape: a real
        # row above the mark whose text is NOT ours. It must never be
        # confirmed by - this request keeps waiting and times out unconfirmed.
        self._add_row(text="a manual send from the Mac itself", offset_ms=1000)
        result = self._wait(expected_text="hello", timeout_s=1.0)
        self.assertEqual(result["outcome"], "unconfirmed")

    def test_a_foreign_rows_error_is_never_reported_as_this_sends_failure(self):
        # Same scenario, but the foreign row has a nonzero error. The old
        # fallback would have reported THIS request as "failed" using
        # someone else's error - exactly the false-outcome risk the review
        # flagged. It must still come back unconfirmed, not failed.
        self._add_row(text="unrelated", offset_ms=1000, error=99)
        result = self._wait(expected_text="hello", timeout_s=1.0)
        self.assertEqual(result["outcome"], "unconfirmed")

    def test_decodes_attributedbody_when_text_is_null(self):
        # message.text can legitimately be NULL with the body only in
        # attributedBody (get_messages' own note). An exact match against the
        # DECODED body still confirms the send - this is not the rejected
        # "newest candidate" fallback, it is the bridge's existing inbound
        # decoder applied to an outbound row.
        blob = make_attributed_body_blob(b"hello")
        rowid = self._add_row(text=None, attributed_body=blob)
        result = self._wait(expected_text="hello")
        self.assertEqual(result["outcome"], "sent")
        self.assertEqual(result["message_rowid"], rowid)

    def test_undecodable_attributedbody_is_ignored_not_claimed(self):
        # A blob the decoder cannot read (empty, bad header, whatever) must
        # not match anything - it is logged and treated as not matching,
        # same as a plain text mismatch, never a crash and never a claim.
        rowid = self._add_row(text=None, attributed_body=b"not a typedstream blob")
        result = self._wait(expected_text="hello", timeout_s=1.0)
        self.assertEqual(result["outcome"], "unconfirmed")
        # The real row shows up once it actually matches.
        self._set_text(rowid, "hello")
        result = self._wait(timeout_s=1.0)
        self.assertEqual(result["outcome"], "sent")
        self.assertEqual(result["message_rowid"], rowid)

    def _set_text(self, rowid, text):
        self.conn.execute("UPDATE message SET text = ? WHERE ROWID = ?", (text, rowid))
        self.conn.commit()

    # ---- a row appears with an error flag: 502 ----

    def test_row_with_a_nonzero_error_is_failed(self):
        rowid = self._add_row(text="hello", error=22)
        result = self._wait()
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["message_rowid"], rowid)
        self.assertIn("22", result["detail"])

    def test_unreadable_db_is_error(self):
        result = bridge.wait_for_text_message(
            os.path.join(self.tmp, "nope.db"), self.CHAT, self.SENT_AFTER_MS, "hello",
            timeout_s=1.0, poll_s=0.5, now=self._now, sleep=self._sleep,
        )
        self.assertEqual(result["outcome"], "error")

    def test_high_water_mark_reads_the_chats_highest_outgoing_message(self):
        self.assertEqual(bridge.read_message_high_water(self.db_path, self.CHAT), 0)
        rowid = self._add_row()
        self._add_row(chat_rowid=2)   # other chat
        self._add_row(is_from_me=0)   # inbound
        self.assertEqual(bridge.read_message_high_water(self.db_path, self.CHAT), rowid)
        with self.assertRaises(sqlite3.Error):
            bridge.read_message_high_water(os.path.join(self.tmp, "nope.db"), self.CHAT)


class AttachmentIdentityTest(unittest.TestCase):
    """attachment_identity: the value the idempotency key binds attachments
    to, alongside chat_id and text (mc-vhnq7, Richard's review)."""

    def test_no_attachment_is_a_comparable_none(self):
        self.assertIsNone(bridge.attachment_identity(None, None, None))
        self.assertEqual(
            bridge.attachment_identity(None, None, None),
            bridge.attachment_identity(None, None, None),
        )

    def test_attachment_path_identity_is_the_exact_path(self):
        self.assertEqual(
            bridge.attachment_identity("/a/b.png", None, None),
            bridge.attachment_identity("/a/b.png", None, None),
        )
        self.assertNotEqual(
            bridge.attachment_identity("/a/b.png", None, None),
            bridge.attachment_identity("/a/c.png", None, None),
        )

    def test_attachment_b64_identity_depends_on_bytes_and_name(self):
        b64 = base64.b64encode(PNG_BYTES).decode("ascii")
        other_b64 = base64.b64encode(PNG_BYTES + b"\x00").decode("ascii")
        self.assertEqual(
            bridge.attachment_identity(None, b64, "crop.png"),
            bridge.attachment_identity(None, b64, "crop.png"),
        )
        self.assertNotEqual(
            bridge.attachment_identity(None, b64, "crop.png"),
            bridge.attachment_identity(None, other_b64, "crop.png"),
        )
        self.assertNotEqual(
            bridge.attachment_identity(None, b64, "crop.png"),
            bridge.attachment_identity(None, b64, "different.png"),
        )

    def test_path_and_b64_identities_never_collide(self):
        b64 = base64.b64encode(PNG_BYTES).decode("ascii")
        self.assertNotEqual(
            bridge.attachment_identity("/a/b.png", None, None),
            bridge.attachment_identity(None, b64, "b.png"),
        )


class IdempotencyTest(unittest.TestCase):
    """idempotency_begin / idempotency_finish / idempotency_await and the
    eviction rule, directly - no HTTP server (mc-vhnq7, Richard's review).
    The HTTP-level behavior (same key twice, concurrent waiters, 409 on a
    mismatched chat/text/attachment) is covered in SendRouteTest."""

    def setUp(self):
        self._orig_keys = dict(bridge._IDEMPOTENCY_KEYS)
        bridge._IDEMPOTENCY_KEYS.clear()

    def tearDown(self):
        bridge._IDEMPOTENCY_KEYS.clear()
        bridge._IDEMPOTENCY_KEYS.update(self._orig_keys)

    def test_a_fresh_key_is_owned_by_the_first_caller(self):
        role, entry = bridge.idempotency_begin("k1", ("chat-x", "hi", None))
        self.assertEqual(role, "owner")
        self.assertFalse(entry["event"].is_set())

    def test_the_same_key_and_identity_again_while_in_flight_is_wait(self):
        role1, entry1 = bridge.idempotency_begin("k2", ("chat-x", "hi", None))
        role2, entry2 = bridge.idempotency_begin("k2", ("chat-x", "hi", None))
        self.assertEqual(role1, "owner")
        self.assertEqual(role2, "wait")
        self.assertIs(entry1, entry2)

    def test_the_same_key_with_different_text_is_conflict(self):
        bridge.idempotency_begin("k3", ("chat-x", "hi", None))
        role, entry = bridge.idempotency_begin("k3", ("chat-x", "bye", None))
        self.assertEqual(role, "conflict")
        self.assertIsNone(entry)

    def test_the_same_key_with_a_different_chat_is_also_conflict(self):
        bridge.idempotency_begin("k4", ("chat-x", "hi", None))
        role, _ = bridge.idempotency_begin("k4", ("chat-y", "hi", None))
        self.assertEqual(role, "conflict")

    def test_the_same_key_with_a_different_attachment_is_also_conflict(self):
        bridge.idempotency_begin("k5", ("chat-x", "hi", ("path", "/a.png")))
        role, _ = bridge.idempotency_begin("k5", ("chat-x", "hi", ("path", "/b.png")))
        self.assertEqual(role, "conflict")

    def test_finish_then_begin_returns_wait_with_the_outcome_available_immediately(self):
        role, entry = bridge.idempotency_begin("k6", ("chat-x", "hi", None))
        self.assertEqual(role, "owner")
        bridge.idempotency_finish("k6", entry, 200, {"status": "sent"})
        role2, entry2 = bridge.idempotency_begin("k6", ("chat-x", "hi", None))
        self.assertEqual(role2, "wait")
        self.assertEqual(bridge.idempotency_await(entry2, timeout=1), (200, {"status": "sent"}))

    def test_a_504_outcome_is_stored_and_replayed_exactly(self):
        # "The stored outcome keeps the exact status and body, including a
        # 504 marked ambiguous." retry_safe: false here, so unlike the
        # retry_safe-true tests below, the entry stays cached.
        role, entry = bridge.idempotency_begin("k7", ("chat-x", "hi", None))
        payload = {"status": "text_unconfirmed", "retry_safe": False, "text_outcome": "unconfirmed"}
        bridge.idempotency_finish("k7", entry, 504, payload)
        self.assertEqual(bridge.idempotency_await(entry, timeout=1), (504, payload))
        role2, entry2 = bridge.idempotency_begin("k7", ("chat-x", "hi", None))
        self.assertEqual(role2, "wait", "a non-retry_safe outcome is still cached for replay")
        self.assertIs(entry2, entry)

    def test_a_retry_safe_outcome_releases_waiters_but_is_not_cached(self):
        # Richard's review: retry_safe means nothing was sent for THIS
        # attempt. Caching it as the key's permanent answer would make
        # every later retry with the same key replay the SAME failure
        # forever instead of ever actually trying the send - exactly
        # backwards from what retry_safe promises. A fresh idempotency_begin
        # with the SAME key, after a retry_safe finish, must come back
        # "owner" again (a NEW entry), not "wait" on the old one.
        role, entry = bridge.idempotency_begin("k9", ("chat-x", "hi", None))
        self.assertEqual(role, "owner")
        payload = {"status": "text_failed", "error": "...", "retry_safe": True}
        bridge.idempotency_finish("k9", entry, 502, payload)

        role2, entry2 = bridge.idempotency_begin("k9", ("chat-x", "hi", None))
        self.assertEqual(role2, "owner", "a retry_safe outcome must not be replayed to the next request")
        self.assertIsNot(entry2, entry, "the retry became a fresh entry, not the dropped one")

    def test_a_retry_safe_finish_still_releases_an_already_blocked_waiter(self):
        # The drop must happen AFTER releasing anyone already waiting -
        # Richard's review: "Waiters already blocked on that key should get
        # that same retry_safe response."
        role, entry = bridge.idempotency_begin("k10", ("chat-x", "hi", None))
        self.assertEqual(role, "owner")
        role2, entry2 = bridge.idempotency_begin("k10", ("chat-x", "hi", None))
        self.assertEqual(role2, "wait")
        self.assertIs(entry2, entry)

        payload = {"status": "text_failed", "error": "...", "retry_safe": True}
        bridge.idempotency_finish("k10", entry, 502, payload)

        # The waiter's own reference still resolves correctly even though
        # the key has already been dropped from the map by this point.
        self.assertEqual(bridge.idempotency_await(entry2, timeout=1), (502, payload))
        self.assertNotIn("k10", bridge._IDEMPOTENCY_KEYS)

    def test_await_returns_none_on_timeout_when_never_finished(self):
        # A waiter must never start its own send just because ITS wait gave
        # up - this is the signal the HTTP layer turns into "answer 504,
        # never run osascript" (SendRouteTest covers that behavior).
        role, entry = bridge.idempotency_begin("k8", ("chat-x", "hi", None))
        self.assertIsNone(bridge.idempotency_await(entry, timeout=0.05))
        self.assertFalse(entry["event"].is_set(), "a timed-out wait must not mark the entry finished")

    def test_an_in_flight_entry_survives_ttl_expiry(self):
        now = time.monotonic()
        bridge._IDEMPOTENCY_KEYS["still-running"] = {
            "identity": ("chat-x", "hi", None), "event": threading.Event(),
            "status": None, "payload": None,
            "created_at": now - bridge.IDEMPOTENCY_TTL_S - 1,  # already past the TTL by age alone
        }
        bridge._idempotency_evict_locked(now)
        self.assertIn(
            "still-running", bridge._IDEMPOTENCY_KEYS,
            "an in-flight entry must never be evicted by age",
        )

    def test_an_in_flight_entry_survives_capacity_eviction(self):
        now = time.monotonic()
        bridge._IDEMPOTENCY_KEYS["still-running"] = {
            "identity": ("chat-x", "hi", None), "event": threading.Event(),
            "status": None, "payload": None, "created_at": now,
        }
        for i in range(bridge.IDEMPOTENCY_MAX_ENTRIES + 10):
            done = threading.Event()
            done.set()
            bridge._IDEMPOTENCY_KEYS["done-%d" % i] = {
                "identity": ("chat-x", "hi-%d" % i, None), "event": done,
                "status": 200, "payload": {}, "created_at": now,
            }
        bridge._idempotency_evict_locked(now)
        self.assertIn(
            "still-running", bridge._IDEMPOTENCY_KEYS,
            "an in-flight entry must never be evicted by capacity",
        )

    def test_completed_entries_are_evicted_past_the_ttl(self):
        now = time.monotonic()
        done = threading.Event()
        done.set()
        bridge._IDEMPOTENCY_KEYS["old-done"] = {
            "identity": ("chat-x", "hi", None), "event": done,
            "status": 200, "payload": {}, "created_at": now - bridge.IDEMPOTENCY_TTL_S - 1,
        }
        bridge._idempotency_evict_locked(now)
        self.assertNotIn("old-done", bridge._IDEMPOTENCY_KEYS)

    def test_completed_entries_are_evicted_down_toward_the_cap(self):
        now = time.monotonic()
        for i in range(bridge.IDEMPOTENCY_MAX_ENTRIES + 5):
            done = threading.Event()
            done.set()
            bridge._IDEMPOTENCY_KEYS["done-%d" % i] = {
                "identity": ("chat-x", "hi-%d" % i, None), "event": done,
                "status": 200, "payload": {}, "created_at": now,
            }
        bridge._idempotency_evict_locked(now)
        self.assertLessEqual(len(bridge._IDEMPOTENCY_KEYS), bridge.IDEMPOTENCY_MAX_ENTRIES)


if __name__ == "__main__":
    unittest.main()
