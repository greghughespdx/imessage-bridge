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
import os
import sqlite3
import stat
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

import bridge


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
    handle_id INTEGER, service TEXT
);
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
CREATE TABLE attachment (
    ROWID INTEGER PRIMARY KEY, guid TEXT, filename TEXT, mime_type TEXT,
    transfer_name TEXT, uti TEXT, total_bytes INTEGER
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
        self._orig_probe = bridge.probe_outgoing_row

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
        bridge.probe_outgoing_row = lambda *a, **k: None

        handler = bridge.make_handler(self.db_path, TEST_TOKEN)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        bridge.send_message = self._orig_send
        bridge.probe_outgoing_row = self._orig_probe
        bridge.OUTBOX_DIR = self._orig_outbox

    def _post(self, payload, token=TEST_TOKEN):
        raw = json.dumps(payload).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers[bridge.AUTH_HEADER] = token
        conn.request("POST", "/send", body=raw, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, json.loads(body)

    def test_text_only_send_is_unchanged(self):
        status, body = self._post({"chat_id": "chat-x", "text": "hello"})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "sent")
        self.assertFalse(body["attachment_sent"])
        self.assertEqual(self.sends[0]["attachment_path"], None)

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


if __name__ == "__main__":
    unittest.main()
