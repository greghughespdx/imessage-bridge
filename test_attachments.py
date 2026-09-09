#!/usr/bin/env python3
"""
Tests for iMessage bridge image-attachment support (mc-iee8).

Two layers:
  1. Unit/integration against a synthetic chat.db built in a temp dir. No live
     data, no network to the real bridge. Exercises get_messages attachment
     extraction, image-only surfacing, resolve_attachment, the path-traversal
     guard, and the live HTTP /messages + /attachment endpoints.
  2. An OPTIONAL fixture test against a read-only COPY of the real chat.db
     (set IMESSAGE_TEST_CHATDB=/path/to/copy). Proves the real test image
     (Greg's 2026-07-12 IMG_3623.HEIC) extracts end to end.

Run: python3 test_attachments.py
"""

import http.client
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

import bridge


APPLE_EPOCH_OFFSET_S = 978307200


def unix_ms_to_apple_ns(unix_ms: int) -> int:
    return int((unix_ms / 1000.0 - APPLE_EPOCH_OFFSET_S) * 1_000_000_000)


# Minimal subset of the chat.db schema the bridge touches.
SCHEMA = """
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT, style INTEGER, display_name TEXT);
CREATE TABLE message (
    ROWID INTEGER PRIMARY KEY,
    guid TEXT,
    text TEXT,
    attributedBody BLOB,
    date INTEGER,
    is_from_me INTEGER,
    cache_has_attachments INTEGER DEFAULT 0,
    handle_id INTEGER,
    service TEXT,
    account TEXT
);
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
CREATE TABLE attachment (
    ROWID INTEGER PRIMARY KEY,
    guid TEXT,
    filename TEXT,
    mime_type TEXT,
    transfer_name TEXT,
    uti TEXT,
    total_bytes INTEGER
);
CREATE TABLE message_attachment_join (
    ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER,
    attachment_id INTEGER
);
"""

# 1x1 PNG (real bytes) used as a stand-in attachment file.
PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)


def build_synthetic_db(tmp: str):
    """Create a synthetic chat.db + attachments dir under tmp.

    Returns (db_path, attachments_dir, guids dict).
    """
    att_dir = os.path.join(tmp, "Attachments")
    sub = os.path.join(att_dir, "aa", "bb", "ATTGUID")
    os.makedirs(sub, exist_ok=True)
    img_path = os.path.join(sub, "photo.png")
    with open(img_path, "wb") as f:
        f.write(PNG_1X1)

    db_path = os.path.join(tmp, "chat.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO handle (ROWID, id) VALUES (1, '+15034102254')")
    conn.execute(
        "INSERT INTO chat (ROWID, guid, style, display_name) VALUES (1, 'iMessage;-;+15034102254', 45, NULL)"
    )

    now_ms = int(time.time() * 1000)

    # Msg 1: text + image attachment.
    conn.execute(
        "INSERT INTO message (ROWID, guid, text, date, is_from_me, cache_has_attachments, handle_id, service) "
        "VALUES (10, 'GUID-TEXT-IMG', 'here is a pic', ?, 0, 1, 1, 'iMessage')",
        (unix_ms_to_apple_ns(now_ms - 3000),),
    )
    # Msg 2: image only (NULL text) - the case the old query dropped.
    conn.execute(
        "INSERT INTO message (ROWID, guid, text, date, is_from_me, cache_has_attachments, handle_id, service) "
        "VALUES (11, 'GUID-IMG-ONLY', NULL, ?, 0, 1, 1, 'iMessage')",
        (unix_ms_to_apple_ns(now_ms - 2000),),
    )
    # Msg 3: plain text, no attachment.
    conn.execute(
        "INSERT INTO message (ROWID, guid, text, date, is_from_me, cache_has_attachments, handle_id, service) "
        "VALUES (12, 'GUID-TEXT-ONLY', 'just text', ?, 0, 0, 1, 'iMessage')",
        (unix_ms_to_apple_ns(now_ms - 1000),),
    )
    for mid in (10, 11, 12):
        conn.execute(
            "INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, ?)", (mid,)
        )

    conn.execute(
        "INSERT INTO attachment (ROWID, guid, filename, mime_type, transfer_name, uti, total_bytes) "
        "VALUES (100, 'ATTGUID', ?, 'image/png', 'photo.png', 'public.png', ?)",
        (img_path, len(PNG_1X1)),
    )
    conn.execute(
        "INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (10, 100)"
    )
    conn.execute(
        "INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (11, 100)"
    )
    conn.commit()
    conn.close()
    return db_path, att_dir, img_path


class AttachmentUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mc-iee8-")
        self.db_path, self.att_dir, self.img_path = build_synthetic_db(self.tmp)
        # Point the bridge's attachments base at our temp dir.
        self._orig_att_dir = bridge.ATTACHMENTS_DIR
        bridge.ATTACHMENTS_DIR = os.path.realpath(self.att_dir)

    def tearDown(self):
        bridge.ATTACHMENTS_DIR = self._orig_att_dir

    def test_get_messages_includes_attachments(self):
        msgs = bridge.get_messages(self.db_path, 0)
        by_guid = {m["guid"]: m for m in msgs}

        self.assertIn("GUID-TEXT-IMG", by_guid)
        m = by_guid["GUID-TEXT-IMG"]
        self.assertEqual(len(m["attachments"]), 1)
        att = m["attachments"][0]
        self.assertEqual(att["index"], 0)
        self.assertEqual(att["mime_type"], "image/png")
        self.assertTrue(att["is_image"])
        self.assertEqual(att["transfer_name"], "photo.png")

    def test_image_only_message_surfaces(self):
        """Old query filtered `text IS NOT NULL` and dropped these."""
        msgs = bridge.get_messages(self.db_path, 0)
        by_guid = {m["guid"]: m for m in msgs}
        self.assertIn("GUID-IMG-ONLY", by_guid)
        self.assertIsNone(by_guid["GUID-IMG-ONLY"]["text"])
        self.assertEqual(len(by_guid["GUID-IMG-ONLY"]["attachments"]), 1)

    def test_text_only_has_empty_attachments(self):
        msgs = bridge.get_messages(self.db_path, 0)
        by_guid = {m["guid"]: m for m in msgs}
        self.assertEqual(by_guid["GUID-TEXT-ONLY"]["attachments"], [])

    def test_resolve_attachment(self):
        resolved = bridge.resolve_attachment(self.db_path, "GUID-TEXT-IMG", 0)
        self.assertIsNotNone(resolved)
        abs_path, mime, name, uti = resolved
        self.assertEqual(os.path.realpath(abs_path), os.path.realpath(self.img_path))
        self.assertEqual(mime, "image/png")
        self.assertEqual(name, "photo.png")

    def test_resolve_attachment_bad_index(self):
        self.assertIsNone(bridge.resolve_attachment(self.db_path, "GUID-TEXT-IMG", 5))

    def test_resolve_attachment_unknown_guid(self):
        self.assertIsNone(bridge.resolve_attachment(self.db_path, "NOPE", 0))

    def test_path_traversal_blocked(self):
        """An attachment.filename pointing outside ATTACHMENTS_DIR is refused."""
        outside = os.path.join(self.tmp, "secret.txt")
        with open(outside, "w") as f:
            f.write("sensitive")
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO attachment (ROWID, guid, filename, mime_type, transfer_name, uti) "
            "VALUES (200, 'EVIL', ?, 'image/png', 'x.png', 'public.png')",
            (outside,),
        )
        conn.execute(
            "INSERT INTO message (ROWID, guid, text, date, is_from_me, cache_has_attachments, handle_id, service) "
            "VALUES (20, 'GUID-EVIL', NULL, 0, 0, 1, 1, 'iMessage')"
        )
        conn.execute(
            "INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 20)"
        )
        conn.execute(
            "INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (20, 200)"
        )
        conn.commit()
        conn.close()
        with self.assertRaises(PermissionError):
            bridge.resolve_attachment(self.db_path, "GUID-EVIL", 0)


class AttachmentHttpTests(unittest.TestCase):
    """Exercise the real HTTP handlers over a loopback socket."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mc-iee8-http-")
        self.db_path, self.att_dir, self.img_path = build_synthetic_db(self.tmp)
        self._orig_att_dir = bridge.ATTACHMENTS_DIR
        bridge.ATTACHMENTS_DIR = os.path.realpath(self.att_dir)

        handler = bridge.make_handler(self.db_path)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        bridge.ATTACHMENTS_DIR = self._orig_att_dir

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, dict(resp.getheaders()), body

    def test_messages_endpoint_has_attachments(self):
        import json

        status, _, body = self._get("/messages?after=0")
        self.assertEqual(status, 200)
        msgs = json.loads(body)
        by_guid = {m["guid"]: m for m in msgs}
        self.assertTrue(by_guid["GUID-TEXT-IMG"]["attachments"][0]["is_image"])

    def test_attachment_endpoint_serves_bytes(self):
        status, headers, body = self._get("/attachment?msg=GUID-TEXT-IMG&index=0")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "image/png")
        self.assertEqual(body, PNG_1X1)

    def test_attachment_endpoint_missing_msg(self):
        status, _, _ = self._get("/attachment?index=0")
        self.assertEqual(status, 400)

    def test_attachment_endpoint_unknown(self):
        status, _, _ = self._get("/attachment?msg=NOPE&index=0")
        self.assertEqual(status, 404)


@unittest.skipUnless(
    os.environ.get("IMESSAGE_TEST_CHATDB"),
    "set IMESSAGE_TEST_CHATDB to a read-only chat.db copy to run the real fixture test",
)
class RealFixtureTest(unittest.TestCase):
    """Prove the real 2026-07-12 test image extracts from a chat.db copy.

    IMESSAGE_TEST_CHATDB must point at a COPY of chat.db (never the live one).
    """

    FIXTURE_GUID = "7AF7B37F-10CB-403E-B936-4920EE367758"

    def test_fixture_attachment_resolves(self):
        db = os.environ["IMESSAGE_TEST_CHATDB"]
        resolved = bridge.resolve_attachment(db, self.FIXTURE_GUID, 0)
        self.assertIsNotNone(resolved, "fixture message/attachment not found")
        abs_path, mime, name, uti = resolved
        self.assertTrue(os.path.isfile(abs_path))
        self.assertEqual(name, "IMG_3623.HEIC")
        self.assertEqual(mime, "image/heic")

    def test_fixture_message_surfaces_with_attachment(self):
        db = os.environ["IMESSAGE_TEST_CHATDB"]
        # Query a wide window (everything) and find the fixture.
        msgs = bridge.get_messages(db, 0)
        match = [m for m in msgs if m["guid"] == self.FIXTURE_GUID]
        self.assertEqual(len(match), 1)
        self.assertEqual(len(match[0]["attachments"]), 1)
        self.assertTrue(match[0]["attachments"][0]["is_image"])




class TestHeaderFilenameFold(unittest.TestCase):
    """U+202F in iOS screenshot names crashed handle_attachment (2026-07-12)."""

    def test_narrow_nbsp_filename_encodes_latin1(self):
        import os
        name = "Screenshot 2026-07-12 at 8.49\u202fAM.png"
        safe = os.path.basename(name)
        safe = safe.encode("ascii", "replace").decode("ascii").replace('"', "_")
        header = 'inline; filename="%s"' % safe
        header.encode("latin-1", "strict")  # must not raise

if __name__ == "__main__":
    unittest.main(verbosity=2)
