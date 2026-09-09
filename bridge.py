#!/usr/bin/env python3
"""
iMessage Bridge HTTP Server
Exposes a minimal HTTP API for reading and sending iMessages via chat.db and AppleScript.

Usage:
    python3 bridge.py [--port 8432] [--db ~/Library/Messages/chat.db]
"""

import argparse
import base64
import binascii
import hmac
import ipaddress
import json
import logging
import logging.handlers
import mimetypes
import os
import platform
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# mc-lkbx diagnostics: timestamped, rotating application log. launchd's
# StandardOut/ErrorPath stays as crash-output-of-last-resort only; routine
# lines go here with rotation so the log can never grow unbounded again
# (the previous stderr-only log reached 792MB).
LOG_PATH = os.environ.get(
    "IMESSAGE_BRIDGE_LOG", "/usr/local/var/log/imessage-bridge-app.log"
)
log = logging.getLogger("bridge")


def setup_logging() -> None:
    log.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z"
    )
    try:
        fh = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=5
        )
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError as e:
        print(f"[bridge] WARNING: cannot open log file {LOG_PATH}: {e}", file=sys.stderr)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    log.addHandler(sh)


# ---------------------------------------------------------------------------
# Shared-secret authentication (mc-btl9u, Greg 2026-09-09)
#
# Every route requires an X-Bridge-Token header matching a secret read once at
# startup from a 0600 file. Before this, anyone who could reach the port could
# read every message on the Mac and send as Greg.
#
# Fail closed in both directions: the bridge refuses to START without a
# readable, correctly-permissioned, non-empty token file, and refuses every
# request whose header does not match. There is no unauthenticated mode and no
# way to turn the check off, because a flag to disable it is a flag that gets
# left on.
# ---------------------------------------------------------------------------

AUTH_HEADER = "X-Bridge-Token"
DEFAULT_TOKEN_FILE = "~/.config/imessage-bridge/token"

# How often one source address may produce an "unauthorized" log line. Without
# this a scanner turns the log into a flood; with it, a real attempt is still
# visible within a minute.
AUTH_LOG_INTERVAL_S = 60.0
# Ceiling on the addresses tracked for that rate limit, so a caller cycling
# source addresses cannot grow the dict without bound.
AUTH_LOG_MAX_TRACKED = 512

_AUTH_LOG_SEEN = {}
_AUTH_LOG_LOCK = threading.Lock()


def token_file_path(env=None) -> str:
    """Absolute path of the token file. IMESSAGE_BRIDGE_TOKEN_FILE overrides."""
    environ = os.environ if env is None else env
    return os.path.expanduser(
        environ.get("IMESSAGE_BRIDGE_TOKEN_FILE") or DEFAULT_TOKEN_FILE
    )


def load_bridge_token(path=None) -> str:
    """Read the shared secret once, at startup.

    Raises RuntimeError with an actionable message if the file is missing,
    world/group readable, or empty. Never logs or returns the value anywhere
    except to the caller.
    """
    path = path or token_file_path()
    try:
        st = os.stat(path)
    except OSError as e:
        raise RuntimeError(
            "cannot read the bridge token file %s: %s. Create it with "
            "`install -m 600 /dev/null %s` and write the shared secret into it, "
            "or set IMESSAGE_BRIDGE_TOKEN_FILE." % (path, e, path)
        )
    if not stat.S_ISREG(st.st_mode):
        raise RuntimeError("bridge token file %s is not a regular file" % path)
    if st.st_mode & 0o077:
        raise RuntimeError(
            "bridge token file %s is group or world accessible (mode %o). "
            "Run `chmod 600 %s`." % (path, stat.S_IMODE(st.st_mode), path)
        )
    try:
        with open(path, "r") as f:
            token = f.read().strip()
    except OSError as e:
        raise RuntimeError("cannot read the bridge token file %s: %s" % (path, e))
    if not token:
        raise RuntimeError("bridge token file %s is empty" % path)
    return token


def _token_bytes(value):
    """Wire bytes for a token value, or None when it has no wire form.

    hmac.compare_digest refuses two str arguments that are not both ASCII and
    raises TypeError. A request header is attacker-controlled, so that TypeError
    was a 500-or-worse on any non-ASCII X-Bridge-Token (finding 2). Comparing
    bytes has no such restriction.

    http.server decodes request headers as latin-1, so encoding back with
    latin-1 recovers exactly the bytes the client put on the wire. Anything that
    cannot be encoded that way (a str carrying codepoints above U+00FF, which
    only a direct caller can produce) returns None and is treated as a
    mismatch: fail closed, never raise.

    The shared secret itself should be ASCII. A non-ASCII secret still compares
    correctly against a client that sends the same latin-1 bytes, but the
    UTF-8-on-the-wire spelling would not match, so keep it ASCII.
    """
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if not isinstance(value, str):
        return None
    try:
        return value.encode("latin-1")
    except UnicodeEncodeError:
        return None


def token_matches(expected: str, presented) -> bool:
    """Constant-time comparison. A missing or undecodable header is a
    non-match, never an exception."""
    if not expected or not presented:
        return False
    expected_bytes = _token_bytes(expected)
    presented_bytes = _token_bytes(presented)
    if expected_bytes is None or presented_bytes is None:
        return False
    return hmac.compare_digest(expected_bytes, presented_bytes)


def should_log_auth_failure(addr: str, now=None) -> bool:
    """True at most once per source address per AUTH_LOG_INTERVAL_S."""
    now = time.time() if now is None else now
    with _AUTH_LOG_LOCK:
        last = _AUTH_LOG_SEEN.get(addr)
        if last is not None and now - last < AUTH_LOG_INTERVAL_S:
            return False
        if len(_AUTH_LOG_SEEN) >= AUTH_LOG_MAX_TRACKED:
            stale = [
                a for a, t in _AUTH_LOG_SEEN.items()
                if now - t >= AUTH_LOG_INTERVAL_S
            ]
            for a in stale:
                del _AUTH_LOG_SEEN[a]
            if len(_AUTH_LOG_SEEN) >= AUTH_LOG_MAX_TRACKED:
                _AUTH_LOG_SEEN.clear()
        _AUTH_LOG_SEEN[addr] = now
        return True


# ---------------------------------------------------------------------------
# Bind address (mc-btl9u)
#
# The bridge used to listen on 0.0.0.0, which is every interface the Mac has,
# including any VPN or guest network it later joins. It now binds one address.
# ---------------------------------------------------------------------------

# Kept for the error text and for callers that want the obvious spellings.
# It is NOT the refusal: a string blocklist cannot be one, because the OS
# accepts many more spellings of the unspecified address than a set can list
# (`0`, `00`, `00000000` and `0x0` all bind 0.0.0.0 on macOS). The parse in
# canonical_bind_address is the refusal.
WILDCARD_ADDRESSES = {"0.0.0.0", "::", "*", ""}


def detect_lan_ipv4() -> str:
    """This host's LAN IPv4, or 127.0.0.1 when there is no usable one.

    Opens a UDP socket toward a TEST-NET-1 address and asks the kernel which
    local address it would use. No packets are sent and nothing is contacted.
    The answer is parsed with `ipaddress` before it is returned, so a garbled
    or unspecified answer becomes the loopback fallback rather than a bind
    string nobody checked.

    CAVEAT: this follows the default route. Under a full-tunnel VPN the default
    route is the VPN, so the address returned here is the VPN address and the
    bridge would listen on the tunnel. A host that can be on a VPN must pin
    `--bind <lan address>` rather than rely on detection - the deploy note
    pins it for iMac27 for exactly this reason.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))
        raw = sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()
    try:
        addr = ipaddress.IPv4Address(str(raw).strip())
    except ValueError:
        return "127.0.0.1"
    if addr.is_unspecified or addr.is_loopback:
        return "127.0.0.1"
    return str(addr)


def canonical_bind_address(requested: str) -> str:
    """Canonical text form of one bindable IP literal.

    Raises ValueError for anything that is not an IP literal, and for every
    spelling of the unspecified address.

    Why parsing and not a string set: `socket.bind` hands the string to
    getaddrinfo, which accepts `0`, `00`, `00000000` and `0x0` and resolves
    every one of them to 0.0.0.0. Comparing against a list of spellings misses
    all four. Parsing with `ipaddress` accepts only real literals, and
    `is_unspecified` covers 0.0.0.0, ::, and every equivalent spelling.
    """
    text = requested.strip()
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        raise ValueError(
            "--bind %r is not an IP address literal. Name one address (for "
            "example 192.168.15.12), or omit --bind to use this host's LAN "
            "IPv4." % requested
        )
    if addr.is_unspecified:
        raise ValueError(
            "--bind %r is the unspecified address: it listens on every "
            "interface. Name one address, or omit --bind to use this host's "
            "LAN IPv4." % requested
        )
    return str(addr)


def resolve_bind_address(requested=None) -> str:
    """Resolve the listen address. Never returns a wildcard.

    Raises ValueError when a wildcard is requested explicitly, so a well-meant
    `--bind 0.0.0.0` fails loudly instead of quietly reopening the hole.
    """
    if requested is None:
        return detect_lan_ipv4()
    return canonical_bind_address(requested)


BRIDGE_STARTED_AT = time.time()
SEND_STATS = {"sent": 0, "failed": 0, "last_send_at": None, "last_error": None}
_SEND_STATS_LOCK = threading.Lock()

# Apple epoch is seconds since 2001-01-01 00:00:00 UTC
# Unix epoch is seconds since 1970-01-01 00:00:00 UTC
# Offset = 978307200 seconds
APPLE_EPOCH_OFFSET_S = 978307200

# chat.db stores dates in nanoseconds since Apple epoch
NS_PER_MS = 1_000_000

# Attachments live outside chat.db on disk. attachment.filename is an absolute
# path (sometimes tilde-prefixed) under this directory. Overridable for tests.
ATTACHMENTS_DIR = os.path.realpath(
    os.path.expanduser(
        os.environ.get("IMESSAGE_ATTACHMENTS_DIR", "~/Library/Messages/Attachments")
    )
)

# Only image attachments are surfaced/served for now (mc-iee8). Everything else
# is reported in metadata but not served as bytes.
IMAGE_MIME_PREFIX = "image/"
IMAGE_UTIS = {"public.heic", "public.heif", "public.jpeg", "public.png"}
# Cap a single attachment fetch. Real photos are a few MB; this guards against a
# pathological row pointing at something huge.
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024

# ---------------------------------------------------------------------------
# Outbound image attachments (mc-am50p)
#
# POST /send takes an optional image, either as attachment_path (a path already
# on this host) or attachment_b64 + attachment_name (bytes the caller supplies,
# staged here). Staged bytes land in a 0700 directory as a 0600 file and are
# deleted once osascript returns, success or failure.
# ---------------------------------------------------------------------------

# Where attachment_b64 uploads are staged. Kept under the user's home rather
# than the system temp dir because Messages.app has to be able to read the file
# it is told to send.
OUTBOX_DIR = os.path.realpath(
    os.path.expanduser(
        os.environ.get("IMESSAGE_BRIDGE_OUTBOX_DIR", "~/.imessage-bridge/outbox")
    )
)

# Outbound is images only, matching the inbound rule ("Only image attachments
# are served"). mimetypes on Apple's 3.9 does not know HEIC, so the extension
# set carries the formats it misses.
OUTBOUND_IMAGE_EXTENSIONS = {
    ".bmp", ".gif", ".heic", ".heif", ".jpeg", ".jpg", ".png", ".tif",
    ".tiff", ".webp",
}


class AttachmentRejected(Exception):
    """Caller-supplied attachment failed validation. Maps to HTTP 400."""


def _looks_like_image(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    if ext in OUTBOUND_IMAGE_EXTENSIONS:
        return True
    guessed = mimetypes.guess_type(path)[0]
    return bool(guessed and guessed.startswith(IMAGE_MIME_PREFIX))


def validate_outbound_attachment(path: str) -> str:
    """Check a host path is a sendable image. Returns the path unchanged.

    Raises AttachmentRejected with a caller-safe reason.
    """
    if not os.path.isabs(path):
        raise AttachmentRejected("attachment_path must be an absolute path")
    if not os.path.isfile(path):
        raise AttachmentRejected("attachment_path is not a file on the bridge host")
    if not _looks_like_image(path):
        raise AttachmentRejected("attachment must be an image")
    try:
        size = os.path.getsize(path)
    except OSError as e:
        raise AttachmentRejected("cannot stat attachment: %s" % e)
    if size > MAX_ATTACHMENT_BYTES:
        raise AttachmentRejected(
            "attachment is %d bytes, over the %d byte cap"
            % (size, MAX_ATTACHMENT_BYTES)
        )
    if size == 0:
        raise AttachmentRejected("attachment is empty")
    return path


def stage_outbound_attachment(b64_data: str, name: str) -> str:
    """Write base64 attachment bytes to a 0600 file under OUTBOX_DIR.

    Returns the absolute staged path. The caller owns deleting it.
    """
    if not isinstance(b64_data, str):
        raise AttachmentRejected("attachment_b64 must be a string")
    if not isinstance(name, str) or not name.strip():
        raise AttachmentRejected("attachment_name is required with attachment_b64")

    safe_name = os.path.basename(name.strip()).replace("\x00", "")
    if not safe_name or safe_name in (".", ".."):
        raise AttachmentRejected("attachment_name is not a usable file name")
    if not _looks_like_image(safe_name):
        raise AttachmentRejected("attachment must be an image")

    try:
        raw = base64.b64decode(b64_data, validate=True)
    except (binascii.Error, ValueError) as e:
        raise AttachmentRejected("attachment_b64 is not valid base64: %s" % e)
    if not raw:
        raise AttachmentRejected("attachment is empty")
    if len(raw) > MAX_ATTACHMENT_BYTES:
        raise AttachmentRejected(
            "attachment is %d bytes, over the %d byte cap"
            % (len(raw), MAX_ATTACHMENT_BYTES)
        )

    try:
        os.makedirs(OUTBOX_DIR, mode=0o700, exist_ok=True)
    except OSError as e:
        raise RuntimeError("cannot create outbox dir %s: %s" % (OUTBOX_DIR, e))

    staged = os.path.join(OUTBOX_DIR, "%s-%s" % (uuid.uuid4().hex, safe_name))
    fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
    except OSError:
        discard_staged_attachment(staged)
        raise
    return staged


def discard_staged_attachment(path) -> None:
    """Best-effort delete of a staged attachment. Never raises."""
    if not path:
        return
    try:
        os.unlink(path)
    except OSError as e:
        log.warning("could not delete staged attachment %s: %s", path, e)


def _expand_attachment_path(raw: str) -> str:
    """Expand a chat.db attachment.filename to an absolute realpath."""
    return os.path.realpath(os.path.expanduser(raw))


def _is_under_attachments_dir(abs_path: str) -> bool:
    """True if abs_path is inside ATTACHMENTS_DIR (path-traversal guard)."""
    base = ATTACHMENTS_DIR
    return abs_path == base or abs_path.startswith(base + os.sep)


def _is_image_attachment(mime_type, uti) -> bool:
    if mime_type and mime_type.startswith(IMAGE_MIME_PREFIX):
        return True
    if uti and uti in IMAGE_UTIS:
        return True
    return False


def _query_attachments(cursor, message_rowid: int) -> list:
    """Return raw attachment rows for a message, ordered stably by join ROWID.

    Each row: dict with filename, mime_type, transfer_name, uti.
    """
    cursor.execute(
        """
        SELECT a.filename, a.mime_type, a.transfer_name, a.uti
        FROM attachment a
        JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID
        WHERE maj.message_id = ?
        ORDER BY maj.ROWID ASC
        """,
        (message_rowid,),
    )
    out = []
    for r in cursor.fetchall():
        out.append({
            "filename": r["filename"],
            "mime_type": r["mime_type"],
            "transfer_name": r["transfer_name"],
            "uti": r["uti"],
        })
    return out


def resolve_attachment(db_path: str, msg_guid: str, index: int):
    """Resolve (abs_path, mime_type, transfer_name, uti) for the index-th
    attachment of the message with the given GUID, or None if not found.

    Validates the resolved path is a real file under ATTACHMENTS_DIR. Read-only.
    """
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA query_only = ON")
        cur.execute("SELECT ROWID FROM message WHERE guid = ? LIMIT 1", (msg_guid,))
        row = cur.fetchone()
        if row is None:
            return None
        atts = _query_attachments(cur, row["ROWID"])
    finally:
        conn.close()

    if index < 0 or index >= len(atts):
        return None
    att = atts[index]
    if not att["filename"]:
        return None
    abs_path = _expand_attachment_path(att["filename"])
    if not _is_under_attachments_dir(abs_path):
        raise PermissionError(f"attachment path outside attachments dir: {abs_path}")
    if not os.path.isfile(abs_path):
        return None
    return abs_path, att["mime_type"], att["transfer_name"], att["uti"]


# ---------------------------------------------------------------------------
# attributedBody decoding (mc-yrd7a)
#
# Modern macOS stores the message body in message.attributedBody (an
# NSKeyedArchiver-era "typedstream" blob) and leaves message.text NULL. A read
# path that selects only m.text therefore drops the message entirely.
#
# This is a port of the canonical TypeScript reader at
# mission-control/channels/imessage/attributed-body.ts (mc-pthof), which was
# verified against 280,870 real chat.db rows. Keep the two in lockstep: the
# byte layout, the search window, the error cases and their messages are all
# intentionally identical.
#
# Layout, from a real blob (bytes shown as hex):
#
#   04 0B "streamtyped"        header
#   81 E8 03                   i16 system version 1000
#   ...
#   84 84 84 12 "NSAttributedString" 00
#   84 84 08 "NSObject" 00
#   85 92 84 84 84 08 "NSString" 01
#   94 84 01 2B                type descriptor: one field, '+' (C string)
#   <length> <utf-8 bytes>     the message body
#   86 ...                     end of object, then the attribute runs
#
# "NSString" appears literally exactly once: later strings in the blob are
# shared-object back-references (0x92 <index>) or bare C strings (0x96), so the
# first literal occurrence is always the body.
#
# Failure is loud by contract. An attributedBody we cannot read is a broken read
# path, not an empty message, so every failure raises and the caller logs it.
# Stdlib only, and nothing newer than Python 3.9: the bridge host runs Apple's
# /usr/bin/python3 (3.9.6 on iMac27) with no third-party packages.
# ---------------------------------------------------------------------------


class AttributedBodyDecodeError(Exception):
    """Raised when a non-empty attributedBody blob cannot be read."""


# 04 0B 's' 't' 'r' 'e' 'a' 'm' 't' 'y' 'p' 'e' 'd'
TYPEDSTREAM_HEADER = b"\x04\x0bstreamtyped"

# Class name whose first literal occurrence precedes the message body.
NSSTRING_CLASS = b"NSString"

# Type descriptor introducing the string payload: 0x84 (new type descriptor),
# 0x01 (one byte of type codes), 0x2B ('+', a length-prefixed C string).
STRING_MARKER = b"\x84\x01\x2b"

# How far past "NSString" the '+' marker may sit before we give up.
MARKER_SEARCH_WINDOW = 16

# typedstream integer prefixes: i16 and i32 follow in little-endian order.
I16_PREFIX = 0x81
I32_PREFIX = 0x82


def _read_typedstream_length(blob: bytes, offset: int):
    """Read a typedstream length at offset.

    Returns (value, next_offset) where next_offset is just past the length.
    """
    if offset >= len(blob):
        raise AttributedBodyDecodeError(
            "truncated blob: no length byte after the string marker"
        )
    head = blob[offset]
    if head == I16_PREFIX:
        if offset + 2 >= len(blob):
            raise AttributedBodyDecodeError(
                "truncated blob: i16 length runs past the end"
            )
        return blob[offset + 1] | (blob[offset + 2] << 8), offset + 3
    if head == I32_PREFIX:
        if offset + 4 >= len(blob):
            raise AttributedBodyDecodeError(
                "truncated blob: i32 length runs past the end"
            )
        value = (
            blob[offset + 1]
            | (blob[offset + 2] << 8)
            | (blob[offset + 3] << 16)
            | (blob[offset + 4] << 24)
        )
        return value, offset + 5
    if head >= 0x80:
        raise AttributedBodyDecodeError(
            "unsupported typedstream length prefix 0x%x" % head
        )
    return head, offset + 1


def decode_attributed_body(blob) -> str:
    """Extract the message body from an attributedBody blob.

    Raises AttributedBodyDecodeError on anything it cannot read. A zero-length
    body is a legitimate result (attachment-only messages carry U+FFFC, not an
    empty string, so an empty result is rare but not itself an error).
    """
    if blob is None or len(blob) == 0:
        raise AttributedBodyDecodeError("empty attributedBody blob")
    blob = bytes(blob)

    if not blob.startswith(TYPEDSTREAM_HEADER):
        raise AttributedBodyDecodeError(
            'not a typedstream blob: missing "streamtyped" header'
        )

    class_at = blob.find(NSSTRING_CLASS, len(TYPEDSTREAM_HEADER))
    if class_at < 0:
        raise AttributedBodyDecodeError("no NSString class descriptor in blob")

    search_from = class_at + len(NSSTRING_CLASS)
    search_limit = min(
        len(blob), search_from + MARKER_SEARCH_WINDOW + len(STRING_MARKER)
    )
    marker_at = blob.find(STRING_MARKER, search_from, search_limit)
    if marker_at < 0:
        raise AttributedBodyDecodeError(
            'no 0x84 0x01 "+" string marker within %d bytes of the NSString '
            "class descriptor" % MARKER_SEARCH_WINDOW
        )

    length, start = _read_typedstream_length(blob, marker_at + len(STRING_MARKER))
    end = start + length
    if end > len(blob):
        raise AttributedBodyDecodeError(
            "declared body length %d runs past the end of a %d-byte blob"
            % (length, len(blob))
        )

    try:
        return blob[start:end].decode("utf-8")
    except UnicodeDecodeError as e:
        raise AttributedBodyDecodeError("body is not valid UTF-8: %s" % e)


def message_body(text, attributed_body, on_error=None):
    """Selection rule shared by every read path.

    Prefer the text column when it is non-NULL, otherwise decode
    attributedBody, otherwise there is no body (returns None so an
    attachment-only row keeps the null text the channel already handles).

    on_error receives the AttributedBodyDecodeError so the caller can log it
    loudly; passing no handler re-raises.
    """
    if text is not None:
        return text
    if not attributed_body:
        return None
    try:
        return decode_attributed_body(attributed_body)
    except AttributedBodyDecodeError as e:
        if on_error is None:
            raise
        on_error(e)
        return None


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

        # Surface image-only messages too: those have NULL text and a
        # cache_has_attachments flag. The old `m.text IS NOT NULL` filter
        # silently dropped every attachment-only message (mc-iee8).
        #
        # Modern macOS also leaves m.text NULL and puts the body in
        # m.attributedBody, so the filter must admit those rows too or every
        # such message is dropped silently (mc-yrd7a). Measured on iMac27's own
        # chat.db 2026-09-09: 2,113 rows had NULL text with a non-NULL
        # attributedBody.
        query = """
            SELECT
                m.ROWID AS rowid,
                m.guid,
                m.text,
                m.attributedBody,
                m.date,
                m.is_from_me,
                m.cache_has_attachments,
                h.id AS sender,
                c.guid AS chat_guid
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            LEFT JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            LEFT JOIN chat c ON cmj.chat_id = c.ROWID
            WHERE m.date > ?
              AND (m.text IS NOT NULL
                   OR m.attributedBody IS NOT NULL
                   OR m.cache_has_attachments = 1)
            ORDER BY m.date ASC
        """
        cursor.execute(query, (after_apple_ns,))
        rows = cursor.fetchall()

        # Second pass: resolve attachment metadata for messages that have any.
        # Client fetches bytes by (message guid, attachment index) via
        # GET /attachment; server paths are intentionally NOT exposed.
        attachments_by_rowid = {}
        for row in rows:
            if row["cache_has_attachments"]:
                attachments_by_rowid[row["rowid"]] = _query_attachments(
                    cursor, row["rowid"]
                )
    except sqlite3.OperationalError as e:
        conn.close()
        raise RuntimeError(f"Database query failed: {e}")

    conn.close()

    messages = []
    for row in rows:
        raw_atts = attachments_by_rowid.get(row["rowid"], [])
        attachments = []
        for i, att in enumerate(raw_atts):
            attachments.append({
                "index": i,
                "mime_type": att["mime_type"],
                "transfer_name": att["transfer_name"],
                "is_image": _is_image_attachment(att["mime_type"], att["uti"]),
            })
        def _log_decode_failure(err, guid=row["guid"]):
            # Loud by contract: an unreadable body is a broken read path, not
            # an empty message. Never log the body itself.
            log.error("attributedBody decode failed guid=%s: %s", guid, err)

        messages.append({
            "guid": row["guid"],
            "text": message_body(
                row["text"], row["attributedBody"], _log_decode_failure
            ),
            "date": apple_ns_to_unix_ms(row["date"]),
            "is_from_me": bool(row["is_from_me"]),
            "sender": row["sender"],
            "chat_guid": row["chat_guid"],
            "attachments": attachments,
        })
    return messages


def build_send_script(chat_id: str, text: str, attachment_path=None) -> str:
    """Build the AppleScript for one send.

    Text goes first, then the attachment as a second send to the same chat, so
    the picture arrives under the sentence that explains it (mc-am50p). Either
    half may be omitted; the caller guarantees at least one is present.
    """
    escaped_chat_id = escape_applescript_string(chat_id)

    # No attachment: emit the exact one-liner that has been running on iMac27
    # since 0.1.0. The text path Greg depends on stays byte-for-byte what it
    # was; the tell-block below is reached only when an image is involved.
    if not attachment_path:
        escaped_text = escape_applescript_string(text)
        return (
            f'tell application "Messages" to send "{escaped_text}" '
            f'to chat id "{escaped_chat_id}"'
        )

    lines = [
        'tell application "Messages"',
        f'    set targetChat to chat id "{escaped_chat_id}"',
    ]
    if text:
        lines.append(f'    send "{escape_applescript_string(text)}" to targetChat')
    escaped_path = escape_applescript_string(attachment_path)
    lines.append(f'    send POSIX file "{escaped_path}" to targetChat')
    lines.append("end tell")
    return "\n".join(lines)


def send_message(chat_id: str, text: str, attachment_path=None) -> float:
    """
    Send an iMessage via AppleScript.
    chat_id should be a full chat GUID like 'iMessage;-;+15034102254'.
    attachment_path, when given, is an absolute path on THIS host that already
    passed validate_outbound_attachment; it is sent after the text.
    Returns the AppleScript elapsed time in seconds. Logs per-send timing and
    the osascript exit/stderr detail (mc-lkbx diagnostics).
    """
    script = build_send_script(chat_id, text, attachment_path)

    t0 = time.monotonic()
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    elapsed = time.monotonic() - t0

    if result.returncode != 0:
        with _SEND_STATS_LOCK:
            SEND_STATS["failed"] += 1
            SEND_STATS["last_error"] = result.stderr.strip()[:300]
        log.error(
            "send FAILED chat=%s elapsed=%.2fs osascript_exit=%d stderr=%s",
            chat_id, elapsed, result.returncode, result.stderr.strip()[:300],
        )
        raise RuntimeError(
            f"AppleScript failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    with _SEND_STATS_LOCK:
        SEND_STATS["sent"] += 1
        SEND_STATS["last_send_at"] = time.time()
    log.info(
        "send ok chat=%s elapsed=%.2fs text_len=%d attachment=%s",
        chat_id, elapsed, len(text), bool(attachment_path),
    )
    return elapsed


def probe_outgoing_row(db_path: str, chat_id: str, sent_after_unix_ms: int) -> None:
    """Background probe (mc-lkbx): after a send reports success, verify an
    is_from_me row actually landed in chat.db for that chat. AppleScript can
    return success while Messages.app silently drops the send; this makes that
    class visible in the log instead of invisible."""
    def _probe():
        time.sleep(3.0)
        try:
            uri = f"file:{db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            cur = conn.cursor()
            cur.execute("PRAGMA query_only = ON")
            cur.execute(
                """
                SELECT COUNT(*) FROM message m
                JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
                JOIN chat c ON cmj.chat_id = c.ROWID
                WHERE c.guid = ? AND m.is_from_me = 1 AND m.date > ?
                """,
                (chat_id, unix_ms_to_apple_ns(sent_after_unix_ms)),
            )
            count = cur.fetchone()[0]
            conn.close()
            if count > 0:
                log.info("send-probe ok chat=%s outgoing_rows=%d", chat_id, count)
            else:
                log.warning(
                    "send-probe MISSING chat=%s - AppleScript succeeded but no "
                    "outgoing row in chat.db within 3s (silent drop or slow write)",
                    chat_id,
                )
        except Exception as e:
            log.warning("send-probe error chat=%s: %s", chat_id, e)

    threading.Thread(target=_probe, daemon=True).start()


class BridgeHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the iMessage bridge."""

    # Set by the server after construction
    db_path: str = ""
    auth_token: str = ""

    def log_message(self, format, *args):
        # Route access logs through the rotating, timestamped logger (mc-lkbx)
        log.info("access %s %s", self.address_string(), format % args)

    def send_json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status: int, message: str) -> None:
        self.send_json(status, {"error": message})

    def require_token(self) -> bool:
        """Gate every route on the shared secret (mc-btl9u).

        Returns True when the request may proceed. On failure it has already
        written the 401 response, so the caller just returns.

        The 401 body is the same generic object whether the header was absent
        or wrong, so a caller learns nothing from the difference.
        """
        presented = self.headers.get(AUTH_HEADER)
        if token_matches(self.auth_token, presented):
            return True
        addr = self.client_address[0] if self.client_address else "unknown"
        if should_log_auth_failure(addr):
            # Path and source only. The presented token NEVER reaches the log.
            log.warning(
                "unauthorized request from %s %s %s (further failures from this "
                "address suppressed for %ds)",
                addr, self.command, self.path.split("?", 1)[0],
                int(AUTH_LOG_INTERVAL_S),
            )
        self.send_json(401, {"error": "unauthorized"})
        return False

    def do_GET(self):
        if not self.require_token():
            return

        parsed = urlparse(self.path)

        if parsed.path == "/healthz":
            checks = {"db_readable": False, "messages_app_running": False}
            try:
                uri = f"file:{self.db_path}?mode=ro"
                conn = sqlite3.connect(uri, uri=True)
                conn.execute("SELECT 1 FROM message LIMIT 1")
                conn.close()
                checks["db_readable"] = True
            except Exception as e:
                checks["db_error"] = str(e)[:200]
            try:
                r = subprocess.run(["pgrep", "-x", "Messages"], capture_output=True)
                checks["messages_app_running"] = r.returncode == 0
            except Exception:
                pass
            with _SEND_STATS_LOCK:
                stats = dict(SEND_STATS)
            status = 200 if checks["db_readable"] else 503
            self.send_json(status, {
                "status": "ok" if status == 200 else "degraded",
                "uptime_s": int(time.time() - BRIDGE_STARTED_AT),
                "checks": checks,
                "send_stats": stats,
                "version": "0.2.0",
            })
            return

        if parsed.path == "/attachment":
            self.handle_attachment(parsed)
            return

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
            log.error("read messages failed: %s", e)
            self.send_error_json(500, str(e))
            return

        self.send_json(200, messages)

    def handle_attachment(self, parsed) -> None:
        """Serve the raw bytes of an image attachment.

        GET /attachment?msg=<message-guid>&index=<n>

        Only image attachments are served. The resolved file must live under
        ATTACHMENTS_DIR (path-traversal guard). Read-only; never touches the db.
        """
        params = parse_qs(parsed.query)
        msg_list = params.get("msg", [])
        idx_list = params.get("index", ["0"])

        if not msg_list:
            self.send_error_json(400, "Missing required query parameter: msg")
            return
        msg_guid = msg_list[0]
        try:
            index = int(idx_list[0])
        except ValueError:
            self.send_error_json(400, "Parameter 'index' must be an integer")
            return

        try:
            resolved = resolve_attachment(self.db_path, msg_guid, index)
        except PermissionError as e:
            log.warning("attachment blocked: %s", e)
            self.send_error_json(403, "Attachment path not permitted")
            return
        except RuntimeError as e:
            log.error("attachment lookup failed: %s", e)
            self.send_error_json(500, str(e))
            return

        if resolved is None:
            self.send_error_json(404, "Attachment not found")
            return

        abs_path, mime_type, transfer_name, uti = resolved

        if not _is_image_attachment(mime_type, uti):
            self.send_error_json(415, "Only image attachments are served")
            return

        try:
            size = os.path.getsize(abs_path)
            if size > MAX_ATTACHMENT_BYTES:
                self.send_error_json(413, "Attachment too large")
                return
            with open(abs_path, "rb") as f:
                data = f.read()
        except OSError as e:
            log.error("attachment read failed %s: %s", abs_path, e)
            self.send_error_json(500, "Cannot read attachment")
            return

        self.send_response(200)
        self.send_header("Content-Type", mime_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        if transfer_name:
            # Safe-ish filename hint for the client's cache naming. Headers are
            # latin-1 only; iOS screenshot names carry U+202F (narrow no-break
            # space before AM/PM) which crashed the whole handler with an
            # UnicodeEncodeError (found live 2026-07-12). ASCII-fold the hint;
            # the bytes and mime type are what matter to the client.
            safe = os.path.basename(transfer_name)
            safe = safe.encode("ascii", "replace").decode("ascii").replace('"', "_")
            self.send_header(
                "Content-Disposition", f'inline; filename="{safe}"'
            )
        self.end_headers()
        self.wfile.write(data)
        log.info(
            "served attachment msg=%s index=%d bytes=%d mime=%s",
            msg_guid, index, len(data), mime_type,
        )

    def do_POST(self):
        if not self.require_token():
            return

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
        attachment_path = body.get("attachment_path")
        attachment_b64 = body.get("attachment_b64")
        attachment_name = body.get("attachment_name")

        if not chat_id:
            self.send_error_json(400, "Missing required field: chat_id")
            return
        if attachment_path is not None and attachment_b64 is not None:
            self.send_error_json(
                400, "Pass attachment_path or attachment_b64, not both"
            )
            return

        has_attachment = attachment_path is not None or attachment_b64 is not None
        # text stays required unless an attachment carries the message, so an
        # existing text-only caller still gets the old 400 on a missing field.
        if text is None:
            if not has_attachment:
                self.send_error_json(400, "Missing required field: text")
                return
            text = ""
        if not isinstance(text, str):
            self.send_error_json(400, "Field 'text' must be a string")
            return
        if not text and not has_attachment:
            self.send_error_json(400, "Nothing to send: text is empty and no attachment")
            return

        # Staged bytes get deleted once osascript returns, success or failure.
        staged_path = None
        try:
            if attachment_b64 is not None:
                staged_path = stage_outbound_attachment(
                    attachment_b64, attachment_name
                )
                send_path = staged_path
            elif attachment_path is not None:
                if not isinstance(attachment_path, str):
                    raise AttachmentRejected("Field 'attachment_path' must be a string")
                send_path = validate_outbound_attachment(attachment_path)
            else:
                send_path = None
        except AttachmentRejected as e:
            self.send_error_json(400, str(e))
            return
        except (OSError, RuntimeError) as e:
            log.error("staging attachment failed chat=%s: %s", chat_id, e)
            discard_staged_attachment(staged_path)
            self.send_error_json(500, "Cannot stage attachment")
            return

        sent_at_ms = int(time.time() * 1000) - 2000  # 2s slack for clock/db skew
        try:
            elapsed = send_message(chat_id, str(text), send_path)
        except subprocess.TimeoutExpired:
            with _SEND_STATS_LOCK:
                SEND_STATS["failed"] += 1
                SEND_STATS["last_error"] = "osascript timeout after 60s"
            log.error("send TIMEOUT chat=%s (osascript >60s)", chat_id)
            self.send_error_json(500, "AppleScript timed out after 60s")
            return
        except RuntimeError as e:
            self.send_error_json(500, str(e))
            return
        finally:
            discard_staged_attachment(staged_path)

        probe_outgoing_row(self.db_path, chat_id, sent_at_ms)
        self.send_json(200, {
            "status": "sent",
            "applescript_elapsed_s": round(elapsed, 2),
            "attachment_sent": send_path is not None,
        })


def make_handler(db_path: str, auth_token: str = ""):
    """Return a BridgeHandler subclass with db_path and the token baked in."""
    class Handler(BridgeHandler):
        pass
    Handler.db_path = db_path
    Handler.auth_token = auth_token
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
        # /info is served by this wrapper before the base do_GET runs, so it
        # needs its own gate or it would be the one unauthenticated route.
        if not self.require_token():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/info":
            self.send_json(200, {
                "name": name,
                "hostname": socket.gethostname(),
                "port": port,
                "version": "0.2.0",
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
    parser.add_argument(
        "--bind",
        type=str,
        default=None,
        help=(
            "Address to listen on (default: this host's LAN IPv4, or 127.0.0.1 "
            "if there is none). Must be an IP address literal; every spelling "
            "of the unspecified address (0.0.0.0, ::, 0, 0x0) is refused."
        ),
    )
    args = parser.parse_args()

    db_path = os.path.expanduser(args.db)
    service_name = args.name or socket.gethostname()

    setup_logging()
    if not os.path.exists(db_path):
        log.warning("database not found at %s", db_path)

    # Fail closed, before the socket exists: no token, no bridge (mc-btl9u).
    try:
        auth_token = load_bridge_token()
    except RuntimeError as e:
        log.error("refusing to start: %s", e)
        print("[bridge] refusing to start: %s" % e, file=sys.stderr)
        sys.exit(2)

    try:
        bind_addr = resolve_bind_address(args.bind)
    except ValueError as e:
        log.error("refusing to start: %s", e)
        print("[bridge] refusing to start: %s" % e, file=sys.stderr)
        sys.exit(2)

    log.info(
        "starting iMessage bridge name=%s bind=%s port=%d db=%s log=%s "
        "auth=required token_file=%s",
        service_name, bind_addr, args.port, db_path, LOG_PATH, token_file_path(),
    )

    handler = make_handler(db_path, auth_token)
    add_info_endpoint(handler, service_name, args.port)
    server = ThreadingHTTPServer((bind_addr, args.port), handler)

    # Register via Bonjour
    bonjour_proc = None
    if not args.no_bonjour:
        try:
            bonjour_proc = register_bonjour(service_name, args.port)
            print(f"[bridge] Bonjour: advertising as '{service_name}._imessage-bridge._tcp'", file=sys.stderr)
        except FileNotFoundError:
            print(f"[bridge] Bonjour: dns-sd not found, skipping registration", file=sys.stderr)

    print(f"[bridge] Listening on {bind_addr}:{args.port}", file=sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[bridge] Shutting down", file=sys.stderr)
        if bonjour_proc:
            bonjour_proc.terminate()
        server.shutdown()


if __name__ == "__main__":
    main()
