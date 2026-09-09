#!/usr/bin/env python3
"""
Tests for shared-secret auth and the bind address (mc-btl9u).

Every token value here is a throwaway literal. No test reads a real token file,
writes one outside a temp dir, or prints a token anywhere.

Run: python3 -m pytest test_auth.py
"""

import http.client
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

import bridge


TEST_TOKEN = "test-token-not-a-real-secret"
WRONG_TOKEN = "test-token-but-the-wrong-one"

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


class TokenFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mc-btl9u-")
        self.path = os.path.join(self.tmp, "token")

    def _write(self, content, mode=0o600):
        with open(self.path, "w") as f:
            f.write(content)
        os.chmod(self.path, mode)
        return self.path

    def test_reads_a_0600_token_file(self):
        self._write(TEST_TOKEN)
        self.assertEqual(bridge.load_bridge_token(self.path), TEST_TOKEN)

    def test_strips_a_trailing_newline(self):
        # `echo secret > token` is how a human will make this file.
        self._write(TEST_TOKEN + "\n")
        self.assertEqual(bridge.load_bridge_token(self.path), TEST_TOKEN)

    def test_refuses_a_missing_file(self):
        with self.assertRaises(RuntimeError) as ctx:
            bridge.load_bridge_token(os.path.join(self.tmp, "absent"))
        self.assertIn("cannot read the bridge token file", str(ctx.exception))

    def test_refuses_a_group_readable_file(self):
        self._write(TEST_TOKEN, mode=0o640)
        with self.assertRaises(RuntimeError) as ctx:
            bridge.load_bridge_token(self.path)
        self.assertIn("group or world accessible", str(ctx.exception))

    def test_refuses_a_world_readable_file(self):
        self._write(TEST_TOKEN, mode=0o604)
        with self.assertRaises(RuntimeError) as ctx:
            bridge.load_bridge_token(self.path)
        self.assertIn("group or world accessible", str(ctx.exception))

    def test_refuses_an_empty_file(self):
        self._write("   \n")
        with self.assertRaises(RuntimeError) as ctx:
            bridge.load_bridge_token(self.path)
        self.assertIn("is empty", str(ctx.exception))

    def test_refuses_a_directory(self):
        with self.assertRaises(RuntimeError):
            bridge.load_bridge_token(self.tmp)

    def test_no_error_message_contains_the_token(self):
        # A startup failure gets printed and logged. It must never carry the
        # secret with it.
        self._write(TEST_TOKEN, mode=0o644)
        with self.assertRaises(RuntimeError) as ctx:
            bridge.load_bridge_token(self.path)
        self.assertNotIn(TEST_TOKEN, str(ctx.exception))

    def test_env_var_selects_the_path(self):
        env = {"IMESSAGE_BRIDGE_TOKEN_FILE": "/somewhere/else/token"}
        self.assertEqual(bridge.token_file_path(env), "/somewhere/else/token")

    def test_default_path_when_the_env_var_is_unset(self):
        self.assertEqual(
            bridge.token_file_path({}),
            os.path.expanduser("~/.config/imessage-bridge/token"),
        )


class TokenMatchTest(unittest.TestCase):
    def test_matches_the_exact_value(self):
        self.assertTrue(bridge.token_matches(TEST_TOKEN, TEST_TOKEN))

    def test_rejects_a_different_value(self):
        self.assertFalse(bridge.token_matches(TEST_TOKEN, WRONG_TOKEN))

    def test_rejects_a_missing_header(self):
        self.assertFalse(bridge.token_matches(TEST_TOKEN, None))
        self.assertFalse(bridge.token_matches(TEST_TOKEN, ""))

    def test_rejects_a_prefix_of_the_token(self):
        self.assertFalse(bridge.token_matches(TEST_TOKEN, TEST_TOKEN[:-1]))

    def test_an_empty_expected_token_never_matches(self):
        # Belt and braces: a handler constructed without a token must deny
        # everything rather than accept everything.
        self.assertFalse(bridge.token_matches("", ""))
        self.assertFalse(bridge.token_matches("", "anything"))


class AuthFailureLogRateLimitTest(unittest.TestCase):
    def setUp(self):
        bridge._AUTH_LOG_SEEN.clear()

    def tearDown(self):
        bridge._AUTH_LOG_SEEN.clear()

    def test_logs_once_per_address_per_interval(self):
        self.assertTrue(bridge.should_log_auth_failure("10.0.0.1", now=1000.0))
        self.assertFalse(bridge.should_log_auth_failure("10.0.0.1", now=1001.0))
        self.assertFalse(
            bridge.should_log_auth_failure("10.0.0.1", now=1000.0 + 59.9)
        )
        self.assertTrue(
            bridge.should_log_auth_failure("10.0.0.1", now=1000.0 + 60.1)
        )

    def test_rate_limit_is_per_address(self):
        self.assertTrue(bridge.should_log_auth_failure("10.0.0.1", now=1000.0))
        self.assertTrue(bridge.should_log_auth_failure("10.0.0.2", now=1000.0))

    def test_tracking_dict_cannot_grow_without_bound(self):
        for i in range(bridge.AUTH_LOG_MAX_TRACKED * 2):
            bridge.should_log_auth_failure("10.0.%d.%d" % (i // 256, i % 256),
                                           now=1000.0)
        self.assertLessEqual(
            len(bridge._AUTH_LOG_SEEN), bridge.AUTH_LOG_MAX_TRACKED
        )


class BindAddressTest(unittest.TestCase):
    def test_default_is_never_a_wildcard(self):
        addr = bridge.resolve_bind_address(None)
        self.assertNotIn(addr, bridge.WILDCARD_ADDRESSES)
        self.assertNotEqual(addr, "0.0.0.0")

    def test_detected_address_is_never_a_wildcard(self):
        addr = bridge.detect_lan_ipv4()
        self.assertNotIn(addr, bridge.WILDCARD_ADDRESSES)
        self.assertNotEqual(addr, "0.0.0.0")
        # Either a real IPv4 or the loopback fallback, never empty.
        self.assertRegex(addr, r"^\d+\.\d+\.\d+\.\d+$")

    def test_explicit_wildcard_is_refused(self):
        for wildcard in ("0.0.0.0", "::", "*", "", "  "):
            with self.assertRaises(ValueError):
                bridge.resolve_bind_address(wildcard)

    def test_an_explicit_address_is_honored(self):
        self.assertEqual(bridge.resolve_bind_address("127.0.0.1"), "127.0.0.1")
        self.assertEqual(
            bridge.resolve_bind_address(" 192.168.15.12 "), "192.168.15.12"
        )

    # Every spelling of the unspecified address the OS resolves to 0.0.0.0.
    # A string blocklist catches only the first three (finding 1): getaddrinfo
    # happily turns "0", "00", "00000000" and "0x0" into 0.0.0.0 as well.
    WILDCARD_SPELLINGS = (
        "0.0.0.0", "::", "*", "", "  ", "0", "00", "00000000", "0x0",
    )

    def _os_bound_address(self, requested):
        """Bind a real server the way main() does; return the OS's own answer.

        Not a string check: server.server_address is what the kernel recorded
        for the listening socket.
        """
        addr = bridge.resolve_bind_address(requested)
        server = ThreadingHTTPServer((addr, 0), bridge.make_handler("/nonexistent.db"))
        try:
            return server.server_address[0]
        finally:
            server.server_close()

    def test_no_spelling_of_the_wildcard_ever_reaches_a_real_socket(self):
        for spelling in self.WILDCARD_SPELLINGS:
            try:
                bound = self._os_bound_address(spelling)
            except ValueError:
                continue  # refused before any socket existed, which is the ask
            self.fail(
                "--bind %r was accepted and the OS bound %s" % (spelling, bound)
            )

    def test_a_named_address_binds_that_address_and_not_the_wildcard(self):
        bound = self._os_bound_address("127.0.0.1")
        self.assertEqual(bound, "127.0.0.1")
        self.assertNotEqual(bound, "0.0.0.0")

    def test_a_hostname_is_not_an_address(self):
        # Only literals. A name could resolve anywhere, including everywhere.
        with self.assertRaises(ValueError):
            bridge.resolve_bind_address("localhost")


class AuthenticatedRoutesTest(unittest.TestCase):
    """Every route over a real loopback socket, with and without the header."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mc-btl9u-http-")
        self.db_path = os.path.join(self.tmp, "chat.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(MINIMAL_SCHEMA)
        conn.commit()
        conn.close()

        self.sends = []
        self._orig_send = bridge.send_message
        self._orig_probe = bridge.probe_outgoing_row
        bridge.send_message = lambda c, t, a=None: self.sends.append(c) or 0.01
        bridge.probe_outgoing_row = lambda *a, **k: None

        handler = bridge.make_handler(self.db_path, TEST_TOKEN)
        bridge.add_info_endpoint(handler, "test-bridge", 8432)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        bridge._AUTH_LOG_SEEN.clear()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        bridge.send_message = self._orig_send
        bridge.probe_outgoing_row = self._orig_probe
        bridge._AUTH_LOG_SEEN.clear()

    def _request(self, method, path, token, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        if token is not None:
            headers[bridge.AUTH_HEADER] = token
        raw = None
        if body is not None:
            raw = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=raw, headers=headers)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        return resp.status, payload

    # Every route, in one place, so a new route added without a gate shows up
    # as a missing line here.
    GET_ROUTES = [
        "/messages?after=0",
        "/healthz",
        "/info",
        "/attachment?msg=NOPE&index=0",
        "/definitely-not-a-route",
    ]

    def test_every_get_route_is_401_without_a_header(self):
        for path in self.GET_ROUTES:
            status, body = self._request("GET", path, None)
            self.assertEqual(status, 401, path)
            self.assertEqual(json.loads(body), {"error": "unauthorized"}, path)

    def test_every_get_route_is_401_with_a_wrong_header(self):
        for path in self.GET_ROUTES:
            status, body = self._request("GET", path, WRONG_TOKEN)
            self.assertEqual(status, 401, path)
            self.assertEqual(json.loads(body), {"error": "unauthorized"}, path)

    def test_messages_is_200_with_the_right_header(self):
        status, body = self._request("GET", "/messages?after=0", TEST_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), [])

    def test_healthz_is_reachable_with_the_right_header(self):
        status, body = self._request("GET", "/healthz", TEST_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")

    def test_info_is_unchanged_apart_from_needing_the_header(self):
        status, body = self._request("GET", "/info", TEST_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(
            sorted(json.loads(body).keys()),
            ["hostname", "name", "port", "version"],
        )
        self.assertEqual(json.loads(body)["name"], "test-bridge")
        self.assertEqual(json.loads(body)["port"], 8432)

    def test_attachment_route_is_gated(self):
        # 404 rather than 401 proves the request got past the gate.
        status, _ = self._request(
            "GET", "/attachment?msg=NOPE&index=0", TEST_TOKEN
        )
        self.assertEqual(status, 404)

    def test_send_is_401_without_a_header_and_never_reaches_applescript(self):
        status, body = self._request(
            "POST", "/send", None, {"chat_id": "chat-x", "text": "hi"}
        )
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"error": "unauthorized"})
        self.assertEqual(self.sends, [], "an unauthorized POST reached send_message")

    def test_send_is_401_with_a_wrong_header(self):
        status, _ = self._request(
            "POST", "/send", WRONG_TOKEN, {"chat_id": "chat-x", "text": "hi"}
        )
        self.assertEqual(status, 401)
        self.assertEqual(self.sends, [])

    def test_send_is_200_with_the_right_header(self):
        status, body = self._request(
            "POST", "/send", TEST_TOKEN, {"chat_id": "chat-x", "text": "hi"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "sent")
        self.assertEqual(self.sends, ["chat-x"])

    def test_the_401_body_is_identical_for_missing_and_wrong(self):
        _, missing = self._request("GET", "/messages?after=0", None)
        _, wrong = self._request("GET", "/messages?after=0", WRONG_TOKEN)
        self.assertEqual(missing, wrong)

    def test_a_handler_with_no_token_denies_everything(self):
        # Fail closed: make_handler's default must not be an open door.
        handler = bridge.make_handler(self.db_path)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/messages?after=0", headers={bridge.AUTH_HEADER: ""})
            self.assertEqual(conn.getresponse().status, 401)
            conn.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
