#!/usr/bin/env python3
"""
Tests for the attributedBody decoder in bridge.py (mc-yrd7a).

Three layers:
  1. Synthetic blobs, hand-built to the typedstream layout. Covers the happy
     path, every declared error case, and the multi-byte length prefixes.
  2. A synthetic chat.db proving get_messages surfaces a row whose text is
     NULL and whose attributedBody carries the body.
  3. A GROUND TRUTH pass over a read-only COPY of a real chat.db. Every row
     that has BOTH text and attributedBody must decode to exactly its text
     column. Skipped when no copy is available.

The ground-truth copy is found via IMESSAGE_TEST_CHATDB (a path to a copy).
Never point it at a live ~/Library/Messages/chat.db: copy first.

Run: python3 -m pytest test_attributed_body.py
"""

import os
import sqlite3
import tempfile
import unittest

import bridge


APPLE_EPOCH_OFFSET_S = 978307200

# Prefix of a real blob, up to and including the "NSString" class descriptor
# and the 0x84 0x01 '+' type marker. Everything before "NSString" is the
# streamtyped header plus the NSAttributedString/NSObject class chain; the
# decoder only requires the header and the first literal "NSString".
BLOB_PREFIX = (
    b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01\x40\x84\x84\x84\x12"
    b"NSAttributedString\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08"
    b"NSString\x01\x94\x84\x01+"
)

# Attribute-run tail that follows the body in a real blob. Irrelevant to the
# decoder, present so the fixtures are not unrealistically truncated.
BLOB_SUFFIX = b"\x86\x84\x02iI\x01\x00"


def make_blob(body_bytes, length_bytes=None):
    """Build a typedstream blob around body_bytes.

    length_bytes lets a test supply a deliberately wrong or multi-byte length
    prefix; by default a correct single-byte prefix is used.
    """
    if length_bytes is None:
        length_bytes = bytes([len(body_bytes)])
    return BLOB_PREFIX + length_bytes + body_bytes + BLOB_SUFFIX


class DecodeAttributedBodyTest(unittest.TestCase):
    def test_decodes_a_plain_ascii_body(self):
        self.assertEqual(
            bridge.decode_attributed_body(make_blob(b"Hello there")),
            "Hello there",
        )

    def test_decodes_multibyte_utf8(self):
        # Escapes, not literals: the source file stays pure ASCII while
        # the fixture still exercises 2-, 3- and 4-byte UTF-8 sequences.
        body = "caf\u00e9 \u2713 \U0001f680 \u65e5\u672c\u8a9e".encode("utf-8")
        self.assertEqual(
            bridge.decode_attributed_body(make_blob(body)),
            body.decode("utf-8"),
        )

    def test_decodes_an_i16_length_prefix(self):
        body = b"x" * 400
        # Literal 0x81, NOT bridge.I16_PREFIX (finding 6): taking the prefix
        # from the constant under test means mutating the constant leaves the
        # test green, so the test asserts nothing about the wire format.
        blob = make_blob(body, bytes([0x81, 400 & 0xFF, 400 >> 8]))
        self.assertEqual(bridge.decode_attributed_body(blob), "x" * 400)

    def test_decodes_an_i32_length_prefix(self):
        body = b"y" * 70000
        # Literal 0x82, for the same reason as the i16 case above.
        length = bytes(
            [
                0x82,
                70000 & 0xFF,
                (70000 >> 8) & 0xFF,
                (70000 >> 16) & 0xFF,
                (70000 >> 24) & 0xFF,
            ]
        )
        self.assertEqual(bridge.decode_attributed_body(make_blob(body, length)),
                         "y" * 70000)

    def test_the_wire_prefixes_are_0x81_and_0x82(self):
        # Pins the constants to the bytes Apple actually writes. Without this,
        # I16_PREFIX could drift and every other test would follow it.
        self.assertEqual(bridge.I16_PREFIX, 0x81)
        self.assertEqual(bridge.I32_PREFIX, 0x82)

    def test_rejects_an_unknown_length_prefix(self):
        # 0x83 is not a length prefix Apple emits. Guessing at it would be
        # inventing message text, so the decoder refuses.
        blob = make_blob(b"x" * 400, bytes([0x83, 400 & 0xFF, 400 >> 8]))
        with self.assertRaises(bridge.AttributedBodyDecodeError) as ctx:
            bridge.decode_attributed_body(blob)
        self.assertIn("unsupported typedstream length prefix", str(ctx.exception))

    def test_a_body_with_an_i16_length_does_not_decode_as_a_bare_byte(self):
        # The 400-byte i16 case, read with the wrong prefix rule, would take
        # 0x81 as a bare length of 129. Assert the decoded length, not just
        # that something came back.
        body = b"x" * 400
        blob = make_blob(body, bytes([0x81, 400 & 0xFF, 400 >> 8]))
        self.assertEqual(len(bridge.decode_attributed_body(blob)), 400)

    def test_accepts_a_zero_length_body(self):
        self.assertEqual(bridge.decode_attributed_body(make_blob(b"")), "")

    def test_accepts_a_memoryview(self):
        self.assertEqual(
            bridge.decode_attributed_body(memoryview(make_blob(b"hi"))), "hi"
        )

    def test_rejects_an_empty_blob(self):
        with self.assertRaises(bridge.AttributedBodyDecodeError) as ctx:
            bridge.decode_attributed_body(b"")
        self.assertIn("empty attributedBody blob", str(ctx.exception))

    def test_rejects_a_non_typedstream_blob(self):
        with self.assertRaises(bridge.AttributedBodyDecodeError) as ctx:
            bridge.decode_attributed_body(b"bplist00nope")
        self.assertIn("streamtyped", str(ctx.exception))

    def test_rejects_a_blob_without_the_nsstring_class(self):
        with self.assertRaises(bridge.AttributedBodyDecodeError) as ctx:
            bridge.decode_attributed_body(bridge.TYPEDSTREAM_HEADER + b"\x00" * 40)
        self.assertIn("no NSString class descriptor", str(ctx.exception))

    def test_rejects_a_marker_beyond_the_search_window(self):
        far = (
            bridge.TYPEDSTREAM_HEADER
            + b"NSString"
            + b"\x00" * (bridge.MARKER_SEARCH_WINDOW + 4)
            + bridge.STRING_MARKER
            + b"\x02hi"
        )
        with self.assertRaises(bridge.AttributedBodyDecodeError) as ctx:
            bridge.decode_attributed_body(far)
        self.assertIn("string marker within", str(ctx.exception))

    def test_rejects_a_length_running_past_the_end(self):
        blob = BLOB_PREFIX + bytes([100]) + b"short"
        with self.assertRaises(bridge.AttributedBodyDecodeError) as ctx:
            bridge.decode_attributed_body(blob)
        self.assertIn("runs past the end", str(ctx.exception))

    def test_rejects_a_truncated_length_prefix(self):
        with self.assertRaises(bridge.AttributedBodyDecodeError) as ctx:
            bridge.decode_attributed_body(BLOB_PREFIX)
        self.assertIn("no length byte", str(ctx.exception))

    def test_rejects_an_unsupported_length_prefix(self):
        blob = BLOB_PREFIX + b"\x8f" + b"body"
        with self.assertRaises(bridge.AttributedBodyDecodeError) as ctx:
            bridge.decode_attributed_body(blob)
        self.assertIn("unsupported typedstream length prefix", str(ctx.exception))

    def test_rejects_a_body_that_is_not_valid_utf8(self):
        with self.assertRaises(bridge.AttributedBodyDecodeError) as ctx:
            bridge.decode_attributed_body(make_blob(b"\xff\xfe\xfd"))
        self.assertIn("not valid UTF-8", str(ctx.exception))


class MessageBodyTest(unittest.TestCase):
    def test_prefers_the_text_column(self):
        self.assertEqual(
            bridge.message_body("from text", make_blob(b"from blob")), "from text"
        )

    def test_prefers_an_empty_text_column_over_the_blob(self):
        # An empty string is a real value, not a missing one.
        self.assertEqual(bridge.message_body("", make_blob(b"from blob")), "")

    def test_falls_back_to_the_blob(self):
        self.assertEqual(
            bridge.message_body(None, make_blob(b"from blob")), "from blob"
        )

    def test_returns_none_when_there_is_nothing_to_read(self):
        self.assertIsNone(bridge.message_body(None, None))
        self.assertIsNone(bridge.message_body(None, b""))

    def test_reports_a_decode_failure_to_the_handler_and_returns_none(self):
        seen = []
        self.assertIsNone(
            bridge.message_body(None, b"not a typedstream blob", seen.append)
        )
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], bridge.AttributedBodyDecodeError)

    def test_reraises_a_decode_failure_with_no_handler(self):
        with self.assertRaises(bridge.AttributedBodyDecodeError):
            bridge.message_body(None, b"not a typedstream blob")


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
CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
"""


class GetMessagesAttributedBodyTest(unittest.TestCase):
    """The whole point of the port: a NULL-text row must reach the client."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "chat.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO handle (ROWID, id) VALUES (1, 'sender-a')")
        conn.execute(
            "INSERT INTO chat (ROWID, guid, style) VALUES (1, 'iMessage;-;chat-a', 45)"
        )
        base_ns = int((1_700_000_000 - APPLE_EPOCH_OFFSET_S) * 1_000_000_000)
        rows = [
            (1, "guid-text", "plain text row", None),
            (2, "guid-blob", None, make_blob("blob only \u2713".encode("utf-8"))),
            (3, "guid-bad", None, b"not a typedstream blob"),
            (4, "guid-empty", None, None),
        ]
        for i, (rowid, guid, text, blob) in enumerate(rows):
            conn.execute(
                "INSERT INTO message (ROWID, guid, text, attributedBody, date, "
                "is_from_me, cache_has_attachments, handle_id, service) "
                "VALUES (?, ?, ?, ?, ?, 0, 0, 1, 'iMessage')",
                (rowid, guid, text, blob, base_ns + i * 1_000_000_000),
            )
            conn.execute(
                "INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, ?)",
                (rowid,),
            )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _by_guid(self):
        msgs = bridge.get_messages(self.db_path, 0)
        return dict((m["guid"], m) for m in msgs)

    def test_text_rows_are_unchanged(self):
        self.assertEqual(self._by_guid()["guid-text"]["text"], "plain text row")

    def test_null_text_row_with_a_blob_is_surfaced_and_decoded(self):
        by_guid = self._by_guid()
        self.assertIn("guid-blob", by_guid)
        self.assertEqual(by_guid["guid-blob"]["text"], "blob only \u2713")

    def test_undecodable_row_is_surfaced_with_null_text_not_a_fake_empty_string(self):
        # It reaches the client so the channel's BRIDGE SENT EMPTY BODY log
        # fires; it never pretends the message was empty.
        self.assertIsNone(self._by_guid()["guid-bad"]["text"])

    def test_a_row_with_neither_text_nor_blob_is_still_filtered_out(self):
        self.assertNotIn("guid-empty", self._by_guid())


def _ground_truth_db():
    path = os.environ.get("IMESSAGE_TEST_CHATDB")
    if path and os.path.isfile(os.path.expanduser(path)):
        return os.path.expanduser(path)
    return None


@unittest.skipIf(
    _ground_truth_db() is None,
    "set IMESSAGE_TEST_CHATDB to a read-only COPY of a real chat.db",
)
class GroundTruthTest(unittest.TestCase):
    """Every row that has BOTH columns must decode to exactly its text column.

    This is the only test that can prove the port matches Apple's encoder
    rather than matching our own fixtures.
    """

    @classmethod
    def setUpClass(cls):
        db = _ground_truth_db()
        uri = "file:%s?mode=ro" % db
        cls.conn = sqlite3.connect(uri, uri=True)
        cls.conn.row_factory = sqlite3.Row

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_every_row_with_both_columns_round_trips(self):
        cur = self.conn.cursor()
        cur.execute(
            "SELECT ROWID, text, attributedBody FROM message "
            "WHERE text IS NOT NULL AND attributedBody IS NOT NULL"
        )
        tested = matches = mismatches = failures = 0
        first_bad = []
        for row in cur:
            tested += 1
            try:
                decoded = bridge.decode_attributed_body(row["attributedBody"])
            except bridge.AttributedBodyDecodeError as e:
                failures += 1
                if len(first_bad) < 5:
                    first_bad.append("rowid=%s decode failed: %s" % (row["ROWID"], e))
                continue
            if decoded == row["text"]:
                matches += 1
            else:
                mismatches += 1
                # Lengths only. Never put a real message body in test output.
                if len(first_bad) < 5:
                    first_bad.append(
                        "rowid=%s mismatch: decoded %d chars, text %d chars"
                        % (row["ROWID"], len(decoded), len(row["text"]))
                    )
        print(
            "\nground truth: tested=%d matches=%d mismatches=%d decode_failures=%d"
            % (tested, matches, mismatches, failures)
        )
        self.assertGreater(tested, 0, "ground-truth db has no dual-column rows")
        self.assertEqual(
            (mismatches, failures), (0, 0), "; ".join(first_bad)
        )

    def test_null_text_rows_decode(self):
        cur = self.conn.cursor()
        cur.execute(
            "SELECT ROWID, attributedBody FROM message "
            "WHERE text IS NULL AND attributedBody IS NOT NULL"
        )
        total = decoded = failures = 0
        first_bad = []
        for row in cur:
            total += 1
            try:
                bridge.decode_attributed_body(row["attributedBody"])
                decoded += 1
            except bridge.AttributedBodyDecodeError as e:
                failures += 1
                if len(first_bad) < 5:
                    first_bad.append("rowid=%s: %s" % (row["ROWID"], e))
        print(
            "\nNULL-text rows: total=%d decoded=%d failures=%d"
            % (total, decoded, failures)
        )
        self.assertGreater(total, 0, "ground-truth db has no NULL-text rows")
        self.assertEqual(failures, 0, "; ".join(first_bad))


if __name__ == "__main__":
    unittest.main()
