# imessage-bridge

Lightweight iMessage bridge for AI assistants. Reads chat.db directly, sends via AppleScript. Zero dependencies.

## What it does

Exposes iMessage read/send over HTTP on your local network. Designed for Claude Code's channel system but works with anything that speaks HTTP.

The bridge runs as a background service on your Mac. Any client on your network can poll for new messages and send replies through a simple JSON API.

## How it works

- Reads `~/Library/Messages/chat.db` (SQLite, read-only) for incoming messages
- Sends via AppleScript (`tell application "Messages"`)
- Advertises via Bonjour/mDNS for auto-discovery
- Pure Python stdlib, zero pip dependencies

## Requirements

- macOS with iMessage signed in
- Python 3 (ships with macOS since Catalina)

## Permissions

macOS requires two one-time permissions. Both trigger automatically as system prompts the first time they're needed.

**1. Full Disk Access (required before reading messages)**

The bridge needs to read `~/Library/Messages/chat.db`. macOS blocks this unless the process has Full Disk Access.

Go to **System Settings > Privacy & Security > Full Disk Access** and add **Terminal** (or whatever terminal app you use). Click the **+** button and navigate to `/Applications/Utilities/Terminal.app`.

If you skip this step, the bridge will start but return empty results for all message queries. There is no error message. It just silently returns nothing.

**2. Automation permission (required before sending messages)**

The first time the bridge sends a message via AppleScript, macOS will show a prompt: "Python wants to control Messages.app." Click **Allow**.

If you skip this or click Deny, sends will fail with an AppleScript error. You can re-enable it in **System Settings > Privacy & Security > Automation**.

After granting both permissions, the bridge works without further prompts.

## Install

Quick install (downloads and starts the service):

```
bash <(curl -sSL https://raw.githubusercontent.com/greghughespdx/imessage-bridge/main/install.sh)
```

Homebrew:

```
brew tap greghughespdx/tools && brew install imessage-bridge
```

Manual (no service, runs in foreground):

```
python3 bridge.py --port 8432
```

## Usage

**Get messages since a timestamp:**

```
GET /messages?after=<unix_ms>
```

Returns a JSON array of message objects. `after` is a Unix timestamp in milliseconds.

**Send a message:**

```
POST /send
Content-Type: application/json

{"chat_id": "iMessage;-;+1234567890", "text": "Hello"}
```

Returns `{"status": "sent"}` on success.

**Bridge metadata:**

```
GET /info
```

Returns the bridge name, hostname, port, and version. Used by channel servers for discovery.

## Configuration

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | 8432 | Port to listen on |
| `--name` | hostname | Bonjour service name, used for discovery |
| `--db` | ~/Library/Messages/chat.db | Path to chat.db |
| `--no-bonjour` | off | Disable Bonjour/mDNS registration |

The installer reads `IMESSAGE_BRIDGE_PORT` and `IMESSAGE_BRIDGE_NAME` environment variables if set before running install.sh.

## Multiple bridges

Each bridge advertises itself via Bonjour with its `--name`. Channel servers can discover all bridges on the network using mDNS service browsing (`_imessage-bridge._tcp`) and connect to a specific bridge by name. This lets you run bridges on multiple Macs and route messages to the right one.

## Architecture

```
Phone -> iMessage -> Messages.app -> chat.db <- bridge.py -> HTTP -> Claude Code channel server
```

The bridge never writes to chat.db. All reads are read-only SQLite connections. Sends go through the local Messages app via AppleScript, which means iMessage authentication stays with the app and is never exposed to the bridge.

## Privacy

All data stays on your local network. No cloud services, no third-party servers. The bridge only reads your local Messages database and sends via the local Messages app.

## vs BlueBubbles

BlueBubbles hooks into Apple's private frameworks (IMCore), requires SIP modifications on newer macOS, and has stability issues on recent OS versions.

imessage-bridge uses two public, stable interfaces: chat.db (SQLite) and AppleScript. Both have worked reliably for over a decade.

The tradeoff: no read receipts, typing indicators, or attachment downloads. For text send/receive, this approach is simpler and more reliable.

## License

MIT. See LICENSE.
