#!/usr/bin/env python3
"""
iMessage Bridge HTTP Server
Exposes a minimal HTTP API for reading and sending iMessages via chat.db and AppleScript.

Usage:
    python3 bridge.py [--port 8432] [--db ~/Library/Messages/chat.db]
"""

import argparse
import json
import os
import platform
import socket
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# Apple epoch is seconds since 2001-01-01 00:00:00 UTC
# Unix epoch is seconds since 1970-01-01 00:00:00 UTC
# Offset = 978307200 seconds
APPLE_EPOCH_OFFSET_S = 978307200

# chat.db stores dates in nanoseconds since Apple epoch
NS_PER_MS = 1_000_000


def unix_ms_to_apple_ns(unix_ms: int) -> int:
    """Convert Unix milliseconds to Apple epoch nanoseconds."""
    unix_s = unix_ms / 1000.0
    apple_s = unix_s - APPLE_EPOCH_OFFSET_S
    return int(apple_s * 1_000_000_000)


def apple_ns_to_unix_ms(apple_ns: int) -> int:
    """Convert Apple epoch nanoseconds to Unix milliseconds."""
    apple_s = apple_ns / 1_000_000_000.0
    unix_s = apple_s + APPLE_EPOCH_OFFSET_S
    return int(unix_s * 1000)


def escape_applescript_string(s: str) -> str:
    """Escape a string for safe embedding in an AppleScript quoted string."""
    # In AppleScript strings, backslash and double-quote need escaping
    s = s.replace("\\", "\\\\")
    s = s.replace('"', '\\"')
    return s


def get_messages(db_path: str, after_unix_ms: int) -> list:
    """
    Query chat.db for messages newer than after_unix_ms.
    Returns a list of message dicts.
    """
    after_apple_ns = unix_ms_to_apple_ns(after_unix_ms)

    # Open in read-only URI mode to avoid locking issues
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    except sqlite3.OperationalError as e:
        raise RuntimeError(f"Cannot open database: {e}")

    conn.row_factory = sqlite3.Row

    try:
        cursor = conn.cursor()
        # PRAGMA to be extra safe
        cursor.execute("PRAGMA query_only = ON")

        query = """
            SELECT
                m.guid,
                m.text,
                m.date,
                m.is_from_me,
                h.id AS sender,
                c.guid AS chat_guid
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            LEFT JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            LEFT JOIN chat c ON cmj.chat_id = c.ROWID
            WHERE m.text IS NOT NULL
              AND m.date > ?
            ORDER BY m.date ASC
        """
        cursor.execute(query, (after_apple_ns,))
        rows = cursor.fetchall()
    except sqlite3.OperationalError as e:
        conn.close()
        raise RuntimeError(f"Database query failed: {e}")

    conn.close()

    messages = []
    for row in rows:
        messages.append({
            "guid": row["guid"],
            "text": row["text"],
            "date": apple_ns_to_unix_ms(row["date"]),
            "is_from_me": bool(row["is_from_me"]),
            "sender": row["sender"],
            "chat_guid": row["chat_guid"],
        })
    return messages


def send_message(chat_id: str, text: str) -> None:
    """
    Send an iMessage via AppleScript.
    chat_id should be a full chat GUID like 'iMessage;-;+15034102254'.
    """
    escaped_text = escape_applescript_string(text)
    escaped_chat_id = escape_applescript_string(chat_id)

    script = (
        f'tell application "Messages" to send "{escaped_text}" '
        f'to chat id "{escaped_chat_id}"'
    )

    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"AppleScript failed (exit {result.returncode}): {result.stderr.strip()}"
        )


class BridgeHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the iMessage bridge."""

    # Set by the server after construction
    db_path: str = ""

    def log_message(self, format, *args):
        # Route access logs to stderr
        print(f"[bridge] {self.address_string()} - {format % args}", file=sys.stderr)

    def send_json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status: int, message: str) -> None:
        self.send_json(status, {"error": message})

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path != "/messages":
            self.send_error_json(404, "Not found")
            return

        params = parse_qs(parsed.query)
        after_list = params.get("after", [])

        if not after_list:
            self.send_error_json(400, "Missing required query parameter: after")
            return

        try:
            after_unix_ms = int(after_list[0])
        except ValueError:
            self.send_error_json(400, "Parameter 'after' must be an integer (Unix ms)")
            return

        try:
            messages = get_messages(self.db_path, after_unix_ms)
        except RuntimeError as e:
            print(f"[bridge] ERROR reading messages: {e}", file=sys.stderr)
            self.send_error_json(500, str(e))
            return

        self.send_json(200, messages)

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path != "/send":
            self.send_error_json(404, "Not found")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self.send_error_json(400, "Empty request body")
            return

        try:
            raw = self.rfile.read(content_length)
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self.send_error_json(400, f"Invalid JSON body: {e}")
            return

        chat_id = body.get("chat_id")
        text = body.get("text")

        if not chat_id:
            self.send_error_json(400, "Missing required field: chat_id")
            return
        if text is None:
            self.send_error_json(400, "Missing required field: text")
            return
        if not isinstance(text, str):
            self.send_error_json(400, "Field 'text' must be a string")
            return

        try:
            send_message(chat_id, str(text))
        except RuntimeError as e:
            print(f"[bridge] ERROR sending message: {e}", file=sys.stderr)
            self.send_error_json(500, str(e))
            return

        self.send_json(200, {"status": "sent"})


def make_handler(db_path: str):
    """Return a BridgeHandler subclass with db_path baked in."""
    class Handler(BridgeHandler):
        pass
    Handler.db_path = db_path
    return Handler


def register_bonjour(name: str, port: int) -> subprocess.Popen:
    """Register the bridge via Bonjour/mDNS using macOS built-in dns-sd.

    Advertises as _imessage-bridge._tcp so channel servers can discover
    bridges on the local network automatically. The dns-sd process runs
    until killed. Returns the Popen handle.
    """
    proc = subprocess.Popen(
        ["dns-sd", "-R", name, "_imessage-bridge._tcp", "local", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc


def add_info_endpoint(handler_class, name: str, port: int):
    """Add a /info endpoint that returns bridge metadata for discovery."""
    original_do_GET = handler_class.do_GET

    def do_GET_with_info(self):
        parsed = urlparse(self.path)
        if parsed.path == "/info":
            self.send_json(200, {
                "name": name,
                "hostname": socket.gethostname(),
                "port": port,
                "version": "0.1.0",
            })
            return
        original_do_GET(self)

    handler_class.do_GET = do_GET_with_info


def main():
    parser = argparse.ArgumentParser(
        description="iMessage Bridge HTTP Server"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8432,
        help="Port to listen on (default: 8432)",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=os.path.expanduser("~/Library/Messages/chat.db"),
        help="Path to chat.db (default: ~/Library/Messages/chat.db)",
    )
    parser.add_argument(
        "--name",
        type=str,
        default=None,
        help="Bonjour service name (default: hostname). Used for discovery when multiple bridges are on the network.",
    )
    parser.add_argument(
        "--no-bonjour",
        action="store_true",
        help="Disable Bonjour/mDNS service registration",
    )
    args = parser.parse_args()

    db_path = os.path.expanduser(args.db)
    service_name = args.name or socket.gethostname()

    if not os.path.exists(db_path):
        print(f"[bridge] WARNING: database not found at {db_path}", file=sys.stderr)

    print(f"[bridge] Starting iMessage bridge", file=sys.stderr)
    print(f"[bridge]   name : {service_name}", file=sys.stderr)
    print(f"[bridge]   port : {args.port}", file=sys.stderr)
    print(f"[bridge]   db   : {db_path}", file=sys.stderr)

    handler = make_handler(db_path)
    add_info_endpoint(handler, service_name, args.port)
    server = ThreadingHTTPServer(("0.0.0.0", args.port), handler)

    # Register via Bonjour
    bonjour_proc = None
    if not args.no_bonjour:
        try:
            bonjour_proc = register_bonjour(service_name, args.port)
            print(f"[bridge] Bonjour: advertising as '{service_name}._imessage-bridge._tcp'", file=sys.stderr)
        except FileNotFoundError:
            print(f"[bridge] Bonjour: dns-sd not found, skipping registration", file=sys.stderr)

    print(f"[bridge] Listening on 0.0.0.0:{args.port}", file=sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[bridge] Shutting down", file=sys.stderr)
        if bonjour_proc:
            bonjour_proc.terminate()
        server.shutdown()


if __name__ == "__main__":
    main()
