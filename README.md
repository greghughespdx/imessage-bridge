# imessage-bridge

Send and receive iMessages from anything that speaks HTTP. Zero dependencies, no middleware, no private APIs.

## iMessage conversation with your AI agent

**imessage-bridge** enables programmatic access to send/receive messages via iMessage on macOS. It reads directly from the Messages app database and sends outbound messages via AppleScript. No third-party middleware, no hooking into private APIs and frameworks, no SIP hacks. It leverages the stable public interfaces that Apple has allowed and supported for more than a decade.

While this package was designed to be used with [Claude Code's new (as of mid-March 2026) Channels](https://code.claude.com/docs/en/channels) feature to enable Claude AI assistants to send and receive iMessages, the HTTP API works with anything that speaks the protocol: scripts, bots, home automation, and - of course - other AI tools and agents.

If you want to have a conversation with your agent when you're on CarPlay, iMessage is your option. Otherwise, you're pretty much stuck with listen-only mode and unable to send Siri-dictated messages. And by the way: It's completey unsafe to hold your phone and use it while you drive. Use the hands-free voice assistant, and pay 100% attention to the road and your surroundings.

This system can send and receive text-based messages via iMessage (blue) and SMS if configured (green). attachments/RCS/MMS are not supported. 

## Two components

This project has two components. You'll install component one or both, depending on your setup and needs:

**The bridge** (`bridge.py`) is a lightweight HTTP server that runs on a Mac where the Apple Messages app installed and signed in with the user account you want the AI agent to use. It reads the Messages database and sends messages via AppleScript. One-to-many clients (agents, etc.) on your network can connect to it. Think of it as a local HTTP API for iMessage. It's ~200 lines of Python with zero dependencies beyond the standard Python3 library.

**The channel server** (`channel-server.ts`) is a Claude Code Channels plugin that connects the Messages app to a Claude Code session. Messages arrive as native Channel events, just like Telegram or Slack. It can either read the Messages app database directly (if Claude Code is on the same Mac as the Messages app being used) or connect to a remote bridge (as described above).

## Which component(s) do I need?

**If Claude Code and iMessage are on the same Mac,** you only need the channel server. It reads the Messages app database directly and sends via AppleScript. No bridge, no HTTP, no network. This is the simplest setup. Quick, fast and simple.

**If Claude Code and iMessage are on different Macs,** you need both components. Install the bridge on the Mac with the Messages app you want the agent to send/receive from. Install the channel server on the Mac with Claude Code installed. The channel server will discover the bridge(s) automatically via Bonjour, or you can configure it manually to connect to a specific bridge URL.

**If you want multiple Claude Code instances (or other clients) to access one iMessage account,** install the bridge on the Mac with the signed-in Messages app. Each client can connect to the same bridge. The bridge is your multi-client story: one system "bridging" iMessage to any number of consumers.

**If you want the HTTP API on the same Mac as Claude Code** (for example, to let other machines connect too), just install both components on the same Mac. You only need to do this if you need to connect a remote client(s) to a Messages app running on the same Mac as you Claude Code instance.

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

## Identity: which iMessage account does the agent use?

This is important to understand before installing: The bridge sends and receives messages as whichever Apple ID is signed into the Messages app on the Mac where the bridge runs. If you want your AI agent to have its own iMessage identity (i.e., separate from your personal one), sign into the agent's Apple ID in Messages.app on the Mac running the bridge.

For example: You are looged into your personal iMessage account on your laptop. You set up a second Mac (or a Mac mini) which is signed into a different Apple ID for the agent. You would install the bridge on the Mac where the agent's iMessage account is logged in. The agent can then send/receive messages using it's own assigned identity. Your contacts will see messages from the agent's name and email/number, and replies will go back to the agent as well.

If you run the bridge on a Mac signed into your personal iMessage, the agent with sends as you and can have access to all of your received messages. You may want that or decide it's fine for your use case, but be aware of it.

## SMS (green bubble) support

The bridge handles both iMessage (blue bubble) and SMS/MMS (green bubble) messages through the same interface. The messages you can send sitting at the Mac's keyboard are the same ones the agent can send. Both end up in chat.db and both can be sent via the same AppleScript commands.

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

**Important:** If you have iMessage signed in on the same Mac as Claude Code AND you want the agent to use a different iMessage account on another Mac, you must set `IMESSAGE_BRIDGE_URL` explicitly. Otherwise the channel server will find the local chat.db first and use your personal account instead of the remote bridge. Set it in your `.mcp.json`:

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

**Get messages since a timestamp:**

```
GET /messages?after=<unix_ms>
```

Returns a JSON array of message objects with fields: `guid`, `text`, `date` (unix ms), `is_from_me`, `sender`, `chat_guid`.

**Send a message:**

```
POST /send
Content-Type: application/json

{"chat_id": "iMessage;-;+1234567890", "text": "Hello"}
```

Returns `{"status": "sent"}` on success. The `chat_id` is the iMessage chat GUID. You can find it in the `chat_guid` field of received messages.

**Bridge info:**

```
GET /info
```

Returns `{"name": "...", "hostname": "...", "port": 8432, "version": "0.1.0"}`.

## Configuration

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | 8432 | Port to listen on |
| `--name` | hostname | Bonjour service name for discovery |
| `--db` | ~/Library/Messages/chat.db | Path to the Messages database |
| `--no-bonjour` | off | Disable Bonjour/mDNS advertisement |

The installer also reads `IMESSAGE_BRIDGE_PORT` and `IMESSAGE_BRIDGE_NAME` environment variables.

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

imessage-bridge takes a different approach: It is small, lightweight and does two things: send and receive text-based messages. It reads the Messages database (a plain SQLite file stored on disk) and sends using AppleScript (Apple's official automation interface). These are public, documented, stable interfaces. Third-party tools have read chat.db for over a decade, and AppleScript automation for Messages has been supported since OS X Mountain Lion.

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

Check that the bridge is running: `curl http://localhost:8432/info`. If it responds, the bridge is up but Bonjour registration may have failed. Check the log: `cat /usr/local/var/log/imessage-bridge.log` (brew) or `cat ~/.imessage-bridge/bridge.log` (install script). You can bypass Bonjour by setting `IMESSAGE_BRIDGE_URL` directly.

**"authorization denied" in the bridge log**

Same as the empty results issue: Full Disk Access is not granted for the process reading chat.db.

**Messages are delayed**

The channel server polls every 1 second by default. If messages seem delayed, check that the bridge is responding: `curl http://<bridge-ip>:8432/info`. Network latency on a local LAN should be sub-millisecond.

## License

MIT. See LICENSE.

