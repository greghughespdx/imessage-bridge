# imessage-bridge

Send and receive iMessages from anything that speaks HTTP. Zero dependencies, no middleware, no private APIs.

## iMessage conversations with your Claude Code agent

**imessage-bridge** enables agents to send/receive messages via iMessage on macOS. It reads directly from the Messages.app database and sends outbound messages via AppleScript. No third-party middleware, no hooking into private APIs and frameworks, no SIP hacks. It leverages the stable public interfaces that Apple has allowed and supported for more than a decade.

While this package was designed to be used with [Claude Code's new (as of mid-March 2026) Channels](https://code.claude.com/docs/en/channels) feature to enable Claude AI assistants to send and receive iMessages, the HTTP API also works with anything that speaks the protocol: scripts, bots, home automation, and - of course - other AI tools and agents. 

This system can send and receive text-based messages via iMessage (blue) and SMS if configured (green). Group chats are supported, too. Image attachments work in both directions: `GET /attachment` serves an inbound image out of the Messages attachment store, and `POST /send` takes an outbound image by host path or as base64 bytes. Non-image attachments, RCS and MMS are not supported.

Side note: If you want to have a conversation with your agent when you're on CarPlay, iMessage is your best option. Otherwise, you're pretty much stuck with listen-only mode and unable to send Siri-dictated messages. Use the hands-free voice assistant, and pay 100% attention to the road and your surroundings. 

## Two components

This project has two components. You'll install component one or both, depending on your setup and needs:

**The bridge** (`bridge.py`) is a lightweight HTTP server that runs on a Mac where the Apple Messages app is installed and signed in with the user account you want the AI agent to use. It reads the Messages database and sends messages via AppleScript. One-to-many clients (agents, etc.) on your network can connect to it. Think of it as a local HTTP API for iMessage. It's ~200 lines of Python with zero dependencies beyond the standard Python3 library.

**The channel server** (`channel-server.ts`) is a Claude Code Channels plugin that connects the Messages app to a Claude Code session. Messages arrive as native Channel events, just like Telegram or Slack. It can either read the Messages app database directly (if Claude Code is on the same Mac as the Messages app being used) or connect to a remote bridge (as described above).

## Which component(s) do I need?

**If Claude Code and iMessage are on the same Mac,** you only need the channel server. It reads the Messages app database directly and sends via AppleScript. No bridge, no HTTP, no network. This is the simplest setup. Quick, fast, and simple.

**If Claude Code and iMessage are on different Macs,** you need both components. Install the bridge on the Mac with the Messages app you want the agent to send/receive from. Install the channel server on the Mac with Claude Code installed. The channel server will discover the bridge(s) automatically via Bonjour, or you can configure it manually to connect to a specific bridge URL.

**If you want multiple Claude Code instances (or other clients) to access one iMessage account,** install the bridge on the Mac with the signed-in Messages app. Each client can connect to the same bridge. The bridge is your multi-client story: one system "bridging" iMessage to any number of consumers.

**If you want the HTTP API on the same Mac as Claude Code** (for example, to let other machines connect too), just install both components on the same Mac. You only need to do this if you need to connect a remote client(s) to a Messages app running on the same Mac as your Claude Code instance.

| Setup | Install | How it connects |
|-------|---------|-----------------|
| **One Mac, one Claude Code** | Channel server only | Reads chat.db directly |
| **Claude Code on a different Mac** | Bridge on iMessage Mac, channel server on Claude Code Mac | Channel server polls bridge over HTTP |
| **Multiple Claude Code machines** | Bridge on iMessage Mac, channel server on each Claude Code Mac | All channel servers poll the same bridge |
| **One Mac, want the HTTP API too** | Both on the same Mac | Channel server reads directly, bridge serves other clients |
| **Non Claude Code agents or other clients** | Bridge on the iMessage Mac (the channel server is only used for Claude Code installs) | All clients connect to the bridge's HTTP endpoint (see API Reference, below) |

## Requirements

- macOS with an iMessage account signed into the Messages app
- Python 3 for the bridge (ships with macOS since Catalina)
- Bun for the channel server (only if using Claude Code integration)

## Permissions (do not skip this part)

**macOS requires two one-time permissions the first time you run the service.** Both will trigger as system screen prompts the first time they're needed. Grant them once, and they won't ask again.

**1. Full Disk Access (required to read messages)**

The bridge (or channel server in local mode) needs to read `~/Library/Messages/chat.db`. macOS blocks this unless the process is granted Full Disk Access.

**If you run the bridge manually from Terminal** (or any terminal app), grant Full Disk Access to that terminal app. Go to **System Settings > Privacy & Security > Full Disk Access**, click **+**, and add **Terminal** (at `/Applications/Utilities/Terminal.app`).

**If you run the bridge as a background service** (via `brew services start` or the install script), the bridge runs under launchd, not inside Terminal. Terminal's Full Disk Access does not apply. You need to grant Full Disk Access to **Python itself**. Go to **System Settings > Privacy & Security > Full Disk Access**, click **+**, press **Cmd+Shift+G**, type `/usr/bin/python3`, and add it.

If you skip this step, everything will appear to work, but message queries will return empty results. There will be no error. It will just silently return nothing. This is the most common setup issue.

**2. Automation permission (required to send messages)**

The first time a message is sent via AppleScript, macOS will prompt you with a message that says something like: "Python wants to control Messages.app." Click **Allow** when you see this message.

If you click Deny or dismiss the prompt, message sends will fail with an AppleScript error. You can fix this later in **System Settings > Privacy & Security > Automation**, where you can manually enable it.

**After granting permissions**, use this command to restart the bridge service for them to take effect:

```
brew services restart imessage-bridge
```

## Identity: Which iMessage account does the agent use?

This is important to understand before installing: The bridge sends and receives messages as whichever Apple ID is signed into the Messages app on the Mac where the bridge runs. If you want your AI agent to have its own iMessage identity (i.e., separate from your personal one), sign into the agent's Apple ID in Messages.app on the Mac running the bridge.

For example: You are logged into your personal iMessage account on your laptop. You set up a second Mac (or a Mac mini) which is signed into a different Apple ID for the agent. You would install the bridge on the Mac where the agent's iMessage account is logged in. The agent can then send/receive messages using its own assigned identity. Your contacts will see messages from the agent's name and email/number, and replies will go back to the agent as well.

If you run the bridge on a Mac signed into your personal iMessage, the agent with sends as you and can have access to all of your received messages. You may want that or decide it's fine for your use case, but be aware of it.

## SMS (green bubble) support

The bridge handles both iMessage (blue bubble) and SMS/MMS (green bubble) messages through the same interface. The messages you can send sitting at the Mac's keyboard are the same ones the agent can send. Both end up in chat.db, and both can be sent via the same AppleScript commands.

***Note:*** For SMS relay to work, you need **Text Message Forwarding** enabled on your iPhone: Settings > Messages > Text Message Forwarding, and enable it for the Mac running the bridge. This is a standard Apple feature. 

## Install the bridge

Homebrew (recommended):

```
brew tap greghughespdx/tools
brew install imessage-bridge
brew services start imessage-bridge
```

Quick install (downloads and starts as a background service):

```
bash <(curl -sSL https://raw.githubusercontent.com/greghughespdx/imessage-bridge/main/install.sh)
```

Manual (runs in foreground, no background service):

```
python3 bridge.py --port 8432
```

The bridge runs on port 8432 by default and advertises itself via Bonjour, so channel servers can find it automatically.

## Install the channel server

The channel server is a Claude Code development channel. Load it when starting Claude Code. Note that while the Channels feature is in early release, you must manually enable your own servers using the "--dangerously-load-development-channels" option when you run Claude Code:

```
claude --dangerously-load-development-channels server:imessage
```

Register it in your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "imessage": {
      "command": "bun",
      "args": ["/path/to/channel-server.ts"]
    }
  }
}
```

**Once the channel server is loaded, iMessages arrive in your Claude Code session as channel events, and Claude has a built-in reply tool to respond.** There is no HTTP to write, no API to call. Inbound messages appear automatically, and Claude replies using the `reply` tool with the `chat_id` from the message. It works the same way as the Telegram and Discord channel plugins.

The channel server automatically detects the best connection mode at startup:

1. If `IMESSAGE_BRIDGE_URL` is set, it connects to that bridge directly.
2. If chat.db is accessible locally, it reads directly (no bridge needed).
3. If neither works, it searches for a bridge on the local network via Bonjour.

**Important:** If you have iMessage signed in on the same Mac as Claude Code AND you want the agent to use a different iMessage account on another Mac, you must set `IMESSAGE_BRIDGE_URL` explicitly. Otherwise, the channel server will find the local chat.db first and use your personal account instead of the remote bridge. Set it in your `.mcp.json`:

```json
{
  "mcpServers": {
    "imessage": {
      "command": "bun",
      "args": ["/path/to/channel-server.ts"],
      "env": {
        "IMESSAGE_BRIDGE_URL": "http://192.168.1.100:8432"
      }
    }
  }
}
```

To connect to a specific bridge by name (when multiple bridges are on the network):

```json
{
  "mcpServers": {
    "imessage": {
      "command": "bun",
      "args": ["/path/to/channel-server.ts"],
      "env": {
        "IMESSAGE_BRIDGE_NAME": "YourMacsNameHere"
      }
    }
  }
}
```

## API reference

### Authentication (required on every route)

Every request needs an `X-Bridge-Token` header whose value matches a shared
secret. Without it, or with the wrong value, every route returns `401` with the
body `{"error": "unauthorized"}` - the same body either way, so a caller learns
nothing from the difference.

The secret lives in a file that must be mode `0600`. The bridge reads it once at
startup and **refuses to start** if the file is missing, group/world accessible,
or empty. There is no way to turn the check off.

```bash
# On the bridge host, and on every machine that talks to it:
mkdir -p ~/.config/imessage-bridge
install -m 600 /dev/null ~/.config/imessage-bridge/token
# write the same secret into that file on both ends, then:
chmod 600 ~/.config/imessage-bridge/token
```

`IMESSAGE_BRIDGE_TOKEN_FILE` overrides the location on both ends. Clients in this
repo (`remote-send.ts`, `attachments.ts`, `channel-server.ts`) read it through
`bridge-auth.ts` and send the header automatically; there is no unauthenticated
client path.

**Do not put the token in a curl `-H` argument.** Command-line arguments are
readable by every user on the machine for as long as the process lives (`ps
-axww`), and they land in shell history. Feed curl a config file on stdin
instead - `-K -` - and the secret never becomes an argv entry:

```bash
# Define this once per shell session, then use it in place of curl.
bridge_curl() {
  printf 'header = "X-Bridge-Token: %s"\n' "$(cat ~/.config/imessage-bridge/token)" \
    | curl -sS -K - "$@"
}

bridge_curl "http://BRIDGE_HOST:8432/info"
```

`printf` is a shell builtin, so the token is never an argument to any process;
the pipe hands it straight to curl, which reads `header = ...` from stdin the
same way it reads a `.curlrc`. Every example below omits the header for
readability - run each one through `bridge_curl` rather than adding `-H`.

### Listen address

The bridge binds **one** address, never `0.0.0.0`. By default it resolves this
host's LAN IPv4 and binds that, falling back to `127.0.0.1` when there is no LAN
address. `--bind <address>` overrides it and must be an IPv4 address literal (the server is IPv4-only); it
is parsed with Python's `ipaddress` module, so every spelling of the unspecified
address (`0.0.0.0`, `::`, and also `0`, `00`, `00000000`, `0x0`, all of which
getaddrinfo resolves to 0.0.0.0) is refused at startup rather than silently
accepted.

Detection follows the **default route**. On a machine that can be on a
full-tunnel VPN, the default route is the tunnel, so detection would bind the
VPN address. Pin `--bind <lan address>` on any such host instead of relying on
detection.

**Get messages since a timestamp:**

```
GET /messages?after=<unix_ms>
```

Returns a JSON array of message objects with fields: `guid`, `text`, `date` (unix ms), `is_from_me`, `sender`, `chat_guid`, `attachments`.

`text` is the `message.text` column when it is set. Modern macOS often leaves that column NULL and stores the body in `message.attributedBody`, a binary `typedstream` blob; the bridge decodes it and returns the result in the same `text` field, so callers see one field either way. A message with neither a readable body nor an attachment is not returned at all. A blob the bridge cannot decode is logged as an error and `text` comes back `null` rather than an empty string, so a broken read path never looks like an empty message.

**Send a message:**

```
POST /send
Content-Type: application/json

{"chat_id": "iMessage;-;+1234567890", "text": "Hello"}
```

Returns `{"status": "sent", "applescript_elapsed_s": 0.42, "attachment_sent": false, "text_confirm_s": 0.5}` on success. The `chat_id` is the iMessage chat GUID. You can find it in the `chat_guid` field of received messages.

**A text-only send is confirmed against chat.db, not against osascript (mc-vhnq7).** osascript exiting 0 only means Messages.app accepted the AppleScript; on 2026-10-05 that happened for a text the bridge log and chat.db never show arriving. Before the AppleScript runs, the bridge reads the chat's highest outgoing message ROWID (the high-water mark), under the same per-chat lock an attachment send takes. After it returns, the bridge polls chat.db for a row above that mark whose body EXACTLY matches what was sent - `message.text` directly, or, when that column is NULL, the bridge's own `attributedBody` decoder applied to that row (the same decoder `/messages` already uses for inbound reads) - and holds the response until that row appears, up to `IMESSAGE_BRIDGE_ATTACHMENT_TIMEOUT_S` (default 30s, the same budget and env var the attachment path uses - both wait on the same chat.db write path). A row whose body does not match this send's text is never bound to this request: an earlier version of this bridge fell back to "the newest row above the mark" when nothing matched, which let such a foreign row's outcome stand in for a send this request's own osascript may have dropped, a false "sent" (or a false "failed" carrying someone else's error) of exactly the kind this ticket exists to fix. **This is a text-equality match, not an identity match - it is a known, accepted limitation, not a guarantee.** A manual Messages send, or another process, writing a DIFFERENT text to the same chat during the wait is correctly never bound. But a manual send, another process, or an overlapping request that happens to send the EXACT SAME text to the SAME chat inside this request's wait window is indistinguishable from this request's own row and can still be bound to it - chat.db carries no per-request identifier matching could key on instead. Two overlapping sends of identical text to the same chat are exactly the case a retry of a non-idempotent send already risks (see "retry_safe" below); this limitation does not introduce a new risk so much as it means the bridge cannot detect that one has happened. `message.is_sent` is not consulted as a failure signal: on 2026-09-09 it stayed 0 on texts that had already arrived on the phone. A row that DOES match with a nonzero `message.error` is a definite failure.

**osascript timing out or exiting non-zero does not answer by itself either (mc-vhnq7 review, Richard).** osascript's own exit is a report, not proof: it can time out, or exit non-zero, AFTER it already submitted the text to Messages. A flat 500 here let a retrying caller double-send. So a text-only send runs the SAME chat.db confirmation above - against the SAME pre-send high-water mark - whether osascript exited 0, timed out, or exited non-zero. (An attachment send is unchanged: osascript timing out or exiting non-zero still answers 500 immediately there.)

Three different outcomes, and they mean different things to a caller:

| Result | HTTP | Body | Meaning for the caller |
|--------|------|------|------|
| A matching row appears with `error` 0 | 200 | `{"status": "sent", "attachment_sent": false, "text_confirm_s": 0.5, ...}` | Delivered - even if osascript itself reported a timeout or non-zero exit first; the log says so when it happens. |
| A matching row appears with a nonzero `error` (whether osascript itself exited 0 or reported a failure first), or chat.db is unreadable before osascript runs (nothing was sent) | 502 | `{"status": "text_failed", "error": "text not confirmed: ...", "text_outcome": "failed" \| "error", "text_sent": false, "attachment_sent": false, "retry_safe": true, ...}` | DEFINITE failure. Nothing was delivered - safe to retry. |
| No matching row appears within the timeout, or chat.db is unreadable while polling AFTER osascript ran (whether it exited 0, timed out, or exited non-zero) | 504 | `{"status": "text_unconfirmed", "error": "text not confirmed: ...", "text_outcome": "unconfirmed" \| "error", "text_sent": false, "attachment_sent": false, "retry_safe": false, ...}` | AMBIGUOUS. Messages may still be mid-send, or the row may simply be slow to land. The bridge is not claiming nothing was delivered, only that it could not prove anything was. NEVER retry this - it carries the same duplicate-visible-iMessage risk as retrying after a client-side timeout. |

**`retry_safe` is the field a caller should actually key on, not the status code alone (mc-vhnq7 review, Richard's third pass).** Every error that can happen before osascript ever runs - a missing or malformed field, an attachment staging failure, the pre-send chat.db-read-error above - carries `"retry_safe": true`, on both the text and the attachment path, because nothing was sent and a retry carries no duplicate-send risk. Everything from the moment osascript runs onward does not, including a generic 500 when osascript itself times out or exits non-zero: that used to answer 500 immediately, but osascript can submit a message and only then report the timeout or the non-zero exit, so a flat 500 there does not prove nothing was sent either. A text-only send now runs the same chat.db confirmation in that case too (see above); an attachment send still answers 500 immediately, with no `retry_safe` field, which is correctly read as "not safe" by a caller that honors the field. `/healthz` counts the three text outcomes separately in `send_stats.text_confirmed`, `send_stats.text_failed`, and `send_stats.text_unconfirmed`.

**Send a message with an image:**

`/send` takes one optional image. Two mutually exclusive shapes:

| Field | With | Meaning |
|-------|------|---------|
| `attachment_path` | - | An absolute path **on the bridge host**. Nothing is copied and nothing is deleted. |
| `attachment_b64` | `attachment_name` | The image bytes, base64. The bridge writes them to a `0600` file in its own outbox directory (inside `~/Library/Messages/Attachments/`, the one place the sandboxed Messages.app is allowed to read from), sends it, and confirms the transfer in chat.db; see "staged_file_kept" below for what happens to the file afterwards. |

Use `attachment_b64` unless the caller and the bridge are the same machine.

**An attachment send is confirmed against chat.db, not against osascript.** Before the AppleScript runs, the bridge reads the chat's highest outgoing attachment ROWID (the high-water mark). After it returns, the bridge polls chat.db for the row this send created (ROWID above the mark, and its `filename` or `transfer_name` naming the file that was sent) and holds the response until its `transfer_state` is final, up to `IMESSAGE_BRIDGE_ATTACHMENT_TIMEOUT_S` (default 30s). A completed row from an earlier send is never taken as this send's evidence, and a new row above the mark with no filename (a manual Messages send, another process, a row still being joined) is logged and ignored: it never authorizes `sent` or `failed` for this request, which stays `missing` until a row carries the sent file's name. Attachment sends to the same chat are serialized with a per-chat lock covering the mark, the send, and the confirmation, so two overlapping requests cannot share a baseline; sends to different chats run in parallel as before. If chat.db cannot be read before the send, nothing is sent and the response is a 502 with `attachment_outcome: "error"` and `text_sent: false`:

| Result | HTTP | Body |
|--------|------|------|
| `transfer_state` reached 5 | 200 | `{"status": "sent", "attachment_sent": true, "attachment_transfer_state": 5, "attachment_confirm_s": 1.5, "staged_file_kept": false, ...}` |
| `transfer_state` reached 6, no row appeared, still in flight at the timeout, or chat.db unreadable | 502 | `{"status": "attachment_failed", "error": "attachment not delivered: ...", "attachment_outcome": "failed" \| "missing" \| "timeout" \| "error", "attachment_sent": false, "text_sent": true/false, "attachment_transfer_state": 6/null, ...}` |

`attachment_sent: true` is never returned for a transfer chat.db did not mark finished. A 502 with `text_sent: true` means the text half went out as its own message before the picture failed. `/healthz` counts both outcomes in `send_stats.attachment_sent` and `send_stats.attachment_failed`.

`staged_file_kept: true` means Messages referenced the staged file directly instead of copying it into its own store, so the bridge left the `0600` file in place rather than orphan the transcript row on the Mac. On macOS 15.7.4 this is what happens every time (live test 2026-09-09: the delivered row's `filename` was the staged path), so one file per sent picture stays in the outbox, the same way Messages keeps its own copies of every attachment. The bridge deletes the staged file immediately only when the send fails or Messages made its own copy. A bounded janitor runs after each kept send and removes bridge-staged files (names `<32 hex>-<name>`, regular files owned by the bridge user, direct children only) older than `IMESSAGE_BRIDGE_OUTBOX_RETENTION_DAYS` (default 30), at most 50 per sweep; the transcript row on the Mac for such an old picture then shows a missing image, which is the trade accepted here.

```
bridge_curl -X POST http://BRIDGE_HOST:8432/send \
  -H 'Content-Type: application/json' \
  -d '{"chat_id": "iMessage;-;+1234567890",
       "text": "Is this mark a 7 or a 1?",
       "attachment_path": "/ABSOLUTE/PATH/ON/BRIDGE/HOST/crop.png"}'
```

(`bridge_curl` is the wrapper defined under "Authentication" above; it supplies
the token header without putting it in argv.)

The text is sent first, then the image as a second message to the same chat, so the picture arrives under the sentence that explains it. `text` may be `""` or omitted when an attachment is present; with no attachment it is still required.

Images only (`.png .jpg .jpeg .heic .heif .gif .webp .bmp .tif .tiff`, or anything `mimetypes` calls `image/*`), 50MB cap. Anything else is a `400`.

**Security note:** `attachment_path` will send any image file readable by the bridge process to any chat the caller names, so anyone holding the shared token can read images off that Mac. The token and the single-interface bind are what stand between the LAN and that capability; treat the token the way you would treat an SSH key for that machine, and do not put it in a repo, a log, or a shell history.

**Retrying a send safely - the `X-Idempotency-Key` header (mc-vhnq7, Richard's review):** a network-level failure (a connection reset, a client-side timeout) can happen AFTER the bridge already received the request and may have run osascript - the caller genuinely cannot tell from a dropped connection alone whether anything was sent. Retrying blind risks a duplicate visible iMessage. Retrying safely needs the bridge's cooperation: send the same `X-Idempotency-Key` header (any caller-chosen string; `remote-send.ts` uses `crypto.randomUUID()`) on every attempt at the SAME logical send, and a NEW key for the next one.

- The FIRST request to use a key runs the real send. Every later request with that SAME key - concurrent or sequential - gets that first request's outcome instead of running osascript again: the EXACT status and body the first request would have received, a 504 included. If the first request is still in flight, later requests wait for it (up to `IDEMPOTENCY_WAIT_TIMEOUT_S`, 100s by default - the bridge's own worst-case send+confirm envelope plus margin) rather than starting their own send. A waiter that gives up after that wait answers 504 `text_unconfirmed` / `retry_safe: false` - it **never** starts its own osascript call just because its own wait ran out; the original request keeps running regardless and will resolve the key for the next lookup.
- The key binds to the exact chat, text, AND attachment (by content for `attachment_b64`, by the given path string for `attachment_path`). Reusing a key with any of those different is refused with `409 {"status": "idempotency_conflict", ...}` and nothing is sent - and, having no `retry_safe` field, is never retried by a caller that honors that contract.
- A request with no `X-Idempotency-Key` header behaves exactly as it did before this existed: no deduplication, no waiting, no conflict check.
- The map behind this is bounded (about 500 entries or 15 minutes, whichever a given entry hits first) so a caller that forgets to stop sending keys cannot grow it without bound. Only a COMPLETED entry is ever dropped this way - an in-flight one is never evicted by age or capacity, no matter how long its send is taking.
- **A bridge restart clears this map entirely.** A retry whose key was registered before a restart - however unlikely, given the bridge's own send+confirm envelope is well under a minute and a restart is not a normal mid-request event - will be treated as a fresh key and can therefore still duplicate a send. This is a known, accepted exposure, not a bug: the alternative (persisting the map to disk across restarts) was not worth the complexity for a window this narrow.

**Fetch an inbound image attachment:**

```
GET /attachment?msg=<message-guid>&index=<n>
```

Returns the raw image bytes. Image attachments only, 50MB cap, and the resolved file must live under `~/Library/Messages/Attachments`.

**Bridge info:**

```
GET /info
```

Returns `{"name": "...", "hostname": "...", "port": 8432, "version": "0.2.0"}`.

## Configuration

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | 8432 | Port to listen on |
| `--name` | hostname | Bonjour service name for discovery |
| `--db` | ~/Library/Messages/chat.db | Path to the Messages database |
| `--no-bonjour` | off | Disable Bonjour/mDNS advertisement |
| `--bind` | this host's LAN IPv4 (else 127.0.0.1) | Address to listen on. A wildcard such as `0.0.0.0` is refused. |

The installer also reads `IMESSAGE_BRIDGE_PORT` and `IMESSAGE_BRIDGE_NAME` environment variables.

| Environment variable | Default | Description |
|----------------------|---------|-------------|
| `IMESSAGE_BRIDGE_LOG` | /usr/local/var/log/imessage-bridge-app.log | Rotating application log |
| `IMESSAGE_BRIDGE_TOKEN_FILE` | ~/.config/imessage-bridge/token | Shared secret for the `X-Bridge-Token` header. Must be `0600`. Read by the bridge AND by every client. |
| `IMESSAGE_BRIDGE_OUTBOX_DIR` | ~/Library/Messages/Attachments/imessage-bridge-outbox | Where `attachment_b64` uploads are staged before sending. Files are `0600` and deleted once chat.db confirms the transfer. Messages.app is sandboxed and can only read paths its entitlements grant (`~/Library/Messages/`, `~/Media/`, `~/Downloads`, a few caches); the old default `~/.imessage-bridge/outbox` was outside that grant and every picture sent from it failed with `transfer_state` 6 (mc-am50p, 2026-09-09). Keep any override inside the grant. The directory is checked on every staged send, not only when it is created: it must be a directory owned by the bridge's own user (anything else is refused with an error), and any group or world bits are chmod'ed away to `0700` first, with a warning in the log naming the old mode. |
| `IMESSAGE_BRIDGE_ATTACHMENT_TIMEOUT_S` | 30 | How long `POST /send` waits for chat.db to confirm a send before answering 502: an attachment row reaching a final `transfer_state`, or (mc-vhnq7) a text-only send's message row appearing. Shared by both paths - same chat.db write path, same justified wait budget. |
| `IMESSAGE_BRIDGE_OUTBOX_RETENTION_DAYS` | 30 | Age after which the outbox janitor deletes bridge-staged files that Messages kept referencing (see "staged_file_kept"). |
| `IMESSAGE_ATTACHMENTS_DIR` | ~/Library/Messages/Attachments | Base directory inbound attachments must live under |

## Multiple bridges on one network

Each bridge advertises via Bonjour with its `--name` (defaults to the machine's hostname). Channel servers discover all bridges on the network and can connect to a specific one by name using the `IMESSAGE_BRIDGE_NAME` environment variable.

This lets you run bridges on multiple Macs (different iMessage accounts, shared family machines, work vs. personal) and route each Claude Code instance to the right one.

## How it works

```
Phone/Mac >> iMessage >> Messages.app >> chat.db
                                            |
                                        bridge.py   (reads SQLite, sends via AppleScript)
                                            |
                                        HTTP API
                                            |
                                    channel-server.ts   (polls bridge, delivers to Claude Code)
                                            |
                                   Claude Code session
```

The bridge never writes to chat.db. All database access is read-only. Messages are sent through the Messages app via AppleScript, so iMessage authentication stays with the app and is never exposed to the bridge or any client.

In local mode (Claude Code on the same machine as the Messages app being used), the channel server skips the bridge entirely and reads from chat.db directly. This is why the bridge is not required for a single-box install with Claude Code.

## Privacy

All data stays on your local network: No cloud services, no third-party servers, no data leaves your machines (other than the messages being sent via iMessage, of course). The bridge reads your local Messages database and sends through your local Messages app. The network traffic between the bridge and channel server is plain HTTP on your LAN.

## Why not other apps?

There are other apps available, which hook into Apple's private frameworks (IMCore, IMFoundation) to intercept messages at the process level. This requires SIP modifications on newer macOS versions, is dependent on undocumented APIs that Apple actively locks down, and in some cases can destabilize the Messages app itself.

imessage-bridge takes a different approach: It is small, lightweight, and does two things: send and receive text-based messages. It reads the Messages database (a plain SQLite file stored on disk) and sends using AppleScript (Apple's official automation interface). These are public, documented, stable interfaces. Third-party tools have read chat.db for over a decade, and AppleScript automation for Messages has been supported since OS X Mountain Lion.

The tradeoff is scope. The bigger, heavier apps can do things we can't: read receipts, typing activity indicators, attachment downloads via private API, group chat management, etc. For reading and sending text messages, imessage-bridge is simpler, stable, and requires no modifications to your system other than the one-time permissions grant.

## Group chats

Group chats work out of the box. No special configuration needed.

You can tell whether a message is a DM or a group message from the `chat_id` format:

- **DM**: `iMessage;-;+15551234567` (the `-` means one-to-one)
- **Group**: `iMessage;+;chat123456789` (the `+` means group, followed by a chat identifier)

In a group chat, the `sender` field (in the API) or `from` field (in channel events) identifies which individual sent each message. Reply using the group's `chat_id` to send to the whole group.

## Troubleshooting

**Bridge starts but returns empty message results**

Full Disk Access is not granted. This is the most common issue. See the Permissions section above. Remember: if running as a brew service, you need to grant FDA to `/usr/bin/python3`, not just Terminal. Restart the service after granting: `brew services restart imessage-bridge`.

**Bridge starts but sends fail with AppleScript error**

The Automation permission was denied. Go to **System Settings > Privacy & Security > Automation** and enable Python's access to Messages.app. If the prompt never appeared, try sending a message manually first: `osascript -e 'tell application "Messages" to send "test" to chat id "iMessage;-;+15551234567"'` (use a real chat ID).

**Channel server connects to the wrong iMessage account**

The channel server found a local chat.db before checking for a remote bridge. Set `IMESSAGE_BRIDGE_URL` explicitly in your `.mcp.json` to force remote mode. See the "Install the channel server" section above.

**Bridge is not discoverable via Bonjour**

Check that the bridge is running: `bridge_curl http://<bind address>:8432/info` (the address the bridge was started with, for example `192.168.15.12`; a bridge pinned to its LAN IPv4 does not listen on localhost) (the wrapper from "Authentication" above; a bare curl gets a `401`, which means the bridge is up and you forgot the header). If it responds, the bridge is up but Bonjour registration may have failed. Check the log: `cat /usr/local/var/log/imessage-bridge.log` (brew) or `cat ~/.imessage-bridge/bridge.log` (install script). You can bypass Bonjour by setting `IMESSAGE_BRIDGE_URL` directly.

**"authorization denied" in the bridge log**

Same as the empty results issue: Full Disk Access is not granted for the process reading chat.db.

**Messages are delayed**

The channel server polls every 1 second by default. If messages seem delayed, check that the bridge is responding: `bridge_curl http://<bridge-ip>:8432/info`. Network latency on a local LAN should be sub-millisecond.

## License

MIT. See LICENSE.

