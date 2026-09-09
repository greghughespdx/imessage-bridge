# Deploy: attributedBody decoding, outbound image attachments, bridge auth (mc-yrd7a, mc-am50p, mc-btl9u)

Three changes to `bridge.py`, one deploy. The first two are additive to the HTTP
contract. **The third is a BREAKING change: every client must send a header or
it gets a 401.** Read "Token first" below before touching anything.

- **mc-yrd7a** - `/messages` decodes `message.attributedBody`. Modern macOS leaves
  `message.text` NULL and puts the body in that blob, so the live bridge drops
  those messages silently today. Measured on iMac27's own chat.db 2026-09-09:
  **2,113 rows** have NULL text with a non-NULL attributedBody.
- **mc-am50p** - `POST /send` takes an optional image, either `attachment_path`
  (a path on the bridge host) or `attachment_b64` + `attachment_name`.
- **mc-btl9u** (Greg's option (a), chosen 2026-09-09 12:45) - every route
  requires an `X-Bridge-Token` header, and the listener binds one address
  instead of `0.0.0.0`. Before this, anything that could reach
  `192.168.15.12:8432` could read every message on that Mac and send as Greg.

## Live file, settled before editing

The ticket flagged an unresolved 0.1.0-vs-0.2.0 question. It is resolved:

```
ssh imac27 "readlink -f /usr/local/opt/imessage-bridge/bin/imessage-bridge"
  -> /usr/local/Cellar/imessage-bridge/0.1.0/bin/imessage-bridge
ssh imac27 "md5 /usr/local/Cellar/imessage-bridge/*/bin/imessage-bridge"
  -> babd8b04522a4f9706e16399c3e292f6
git show 3ce9c66:bridge.py | md5
  -> babd8b04522a4f9706e16399c3e292f6
```

The **Cellar directory name is 0.1.0, the file inside it is repo `master`
(3ce9c66), and that file reports `"version": "0.2.0"`** from its own string
constants. The directory name is stale packaging metadata, nothing more. There
is no third copy: `readlink -f` on `/usr/local/opt` lands in that same 0.1.0
directory, and the running process (`ps -axo pid,command`) is launched from the
`/usr/local/opt` symlink.

The live file matches master exactly, so this deploy starts from master with no
divergence to reconcile.

Interpreter: the running process is
`/Applications/Xcode.app/.../Python3.framework/Versions/3.9/.../Python` - Apple's
Python **3.9**, no third-party packages. Everything added here is stdlib and
3.9-clean (`/usr/bin/python3 -m py_compile bridge.py` passes).

## Deployment map

| Component | Runs on | Live path |
|-----------|---------|-----------|
| Bridge (`bridge.py`) | iMac27 (192.168.15.12:8432) | `/usr/local/Cellar/imessage-bridge/0.1.0/bin/imessage-bridge`, via the `/usr/local/opt` symlink |
| LaunchAgent | iMac27 | `~/Library/LaunchAgents/homebrew.mxcl.imessage-bridge.plist` (keepalive, runatload) |
| Channel (consumer) | Mac Studio | `~/Dev/mission-control/channels/imessage/server.ts` |

## Token first, and it is not optional

The bridge **refuses to start** without a readable `0600` token file (exit 2).
Deploying the new binary before creating that file leaves the bridge down, and
the channel-watchdog will kickstart it in a loop without fixing it.

1. Greg creates the shared secret in Vaultwarden. This note deliberately does
   not generate, name, or print a value.
2. The SAME value goes into a `0600` file on **both** hosts, before the binary
   is copied:

```bash
# iMac27 (the bridge)
ssh imac27 'mkdir -p ~/.config/imessage-bridge && install -m 600 /dev/null ~/.config/imessage-bridge/token'
# write the secret into it without it reaching shell history or a transcript,
# e.g. by piping from `bw get password <item>` executed on that host

# Mac Studio (every client: the channel, the watchdog, the alert scripts)
mkdir -p ~/.config/imessage-bridge && install -m 600 /dev/null ~/.config/imessage-bridge/token
```

3. Verify both ends match WITHOUT printing either:

```bash
shasum -a 256 ~/.config/imessage-bridge/token | cut -d' ' -f1
ssh imac27 'shasum -a 256 ~/.config/imessage-bridge/token | cut -d" " -f1'
# the two digests must be identical
```

Rotation later: write the new value on both ends, restart the bridge, restart
the channel. Clients read the file on every request, so only the bridge needs a
restart to pick up a new value.

## Order

Bridge plus clients. The changes in the report's "mission-control changes
required" section must land BEFORE or WITH the bridge restart: an un-updated
client gets a 401 on every call.

- New bridge + old channel is safe for mc-yrd7a **only if the channel already
  handles a null `text`** - which it does since mc-iee8 (`text ?? ''`). What
  changes is that previously-dropped messages now arrive with a real body.
- mc-am50p adds request fields the channel does not send yet, so it is inert
  until a consumer opts in. See "What a consumer must pass" below.

## Deploy (run when Greg is awake - this is the live channel he depends on)

```bash
# 1. Keep the current live file as the rollback artifact, beside itself.
ssh imac27 'cp -p /usr/local/Cellar/imessage-bridge/0.1.0/bin/imessage-bridge \
  /usr/local/Cellar/imessage-bridge/0.1.0/bin/imessage-bridge.pre-mc-yrd7a'
ssh imac27 'md5 /usr/local/Cellar/imessage-bridge/0.1.0/bin/imessage-bridge.pre-mc-yrd7a'
# expect: babd8b04522a4f9706e16399c3e292f6

# 1b. Token file in place on BOTH hosts (see "Token first"), digests matched.
#     The bridge will not start without it.

# 2. Copy the new file into place.
scp bridge.py imac27:/usr/local/Cellar/imessage-bridge/0.1.0/bin/imessage-bridge
ssh imac27 'chmod 755 /usr/local/Cellar/imessage-bridge/0.1.0/bin/imessage-bridge'

# 3. Syntax-check it on the host, with the host's own interpreter, BEFORE restart.
ssh imac27 '/usr/bin/python3 -m py_compile \
  /usr/local/Cellar/imessage-bridge/0.1.0/bin/imessage-bridge && echo COMPILES'

# 4. Restart.
ssh imac27 'launchctl kickstart -k gui/$(id -u)/homebrew.mxcl.imessage-bridge'

# 5. Confirm it actually came up. It exits 2 rather than serving when the token
#    file is absent or has the wrong permissions, so an empty result here means
#    the secret step failed, not the code.
ssh imac27 'pgrep -fl imessage-bridge'
ssh imac27 'tail -5 /usr/local/var/log/imessage-bridge-app.log'

# 6. Confirm the bind address. Default resolves iMac27's own LAN IPv4.
ssh imac27 'lsof -nP -iTCP:8432 -sTCP:LISTEN'
# expect 192.168.15.12:8432 (LISTEN), NOT *:8432
```

If step 6 shows `127.0.0.1` (no default route at boot, say), the channel cannot
reach it. Pin it rather than hoping: add `--bind 192.168.15.12` to the
LaunchAgent's ProgramArguments.

## Rollback

The previous Cellar file, kept in step 1:

```bash
ssh imac27 'cp -p /usr/local/Cellar/imessage-bridge/0.1.0/bin/imessage-bridge.pre-mc-yrd7a \
  /usr/local/Cellar/imessage-bridge/0.1.0/bin/imessage-bridge'
ssh imac27 'launchctl kickstart -k gui/$(id -u)/homebrew.mxcl.imessage-bridge'
```

Equivalent from git if that copy is lost: `git show 3ce9c66:bridge.py`.

The channel-watchdog auto-recovers a bridge that fails to start. It does NOT
catch a bridge that starts and returns wrong data, so step 5 below is the real
gate, not the watchdog.

## Post-restart verification

0. Auth, before anything else:

```bash
TOK=$(cat ~/.config/imessage-bridge/token)
curl -s -o /dev/null -w '%{http_code}\n' http://192.168.15.12:8432/healthz
# expect 401
curl -s -o /dev/null -w '%{http_code}\n' -H 'X-Bridge-Token: wrong' http://192.168.15.12:8432/healthz
# expect 401
curl -s -o /dev/null -w '%{http_code}\n' -H "X-Bridge-Token: $TOK" http://192.168.15.12:8432/healthz
# expect 200
unset TOK
```

1. `/healthz` with the header -> `"status": "ok"`.
2. `curl -s http://192.168.15.12:8432/info` -> responds (version string stays 0.2.0).
3. Read back recent traffic and confirm bodies are present:
   `curl -s "http://192.168.15.12:8432/messages?after=$(( ($(date +%s) - 86400) * 1000 ))" | python3 -c "import json,sys; m=json.load(sys.stdin); print(len(m), 'rows,', sum(1 for x in m if x['text'] is None), 'with null text')"`
   Before this deploy the NULL-text count was the number of *dropped* rows;
   after it, those rows appear with real text and the null count should be near
   zero (attachment-only messages legitimately stay null).
4. `ssh imac27 'grep -c "attributedBody decode failed" /usr/local/var/log/imessage-bridge-app.log'`
   -> expect 0. Any non-zero count is a real defect; capture the guids and roll back.
5. Greg sends a plain text iMessage; confirm it still arrives in the channel unchanged.
6. Outbound image, end to end (this is the one thing no test can prove, because
   the tests deliberately never run osascript):

```bash
curl -X POST http://192.168.15.12:8432/send \
  -H 'Content-Type: application/json' \
  -d '{"chat_id": "iMessage;-;+1XXXXXXXXXX",
       "text": "mc-am50p deploy check",
       "attachment_path": "/ABSOLUTE/PATH/ON/IMAC27/test.png"}'
```

   Confirm the picture actually arrives on the phone, not just a 200. Then repeat
   with `attachment_b64` + `attachment_name` and confirm the staged file is gone:
   `ssh imac27 'ls -la ~/.imessage-bridge/outbox'` -> empty.

   **Watch for one specific failure:** Messages.app is sandboxed. If it cannot
   read the staged file, the text arrives and the image does not. If that
   happens, set `IMESSAGE_BRIDGE_OUTBOX_DIR` to a path Messages can read (add
   `EnvironmentVariables` to the plist) rather than changing code.

## What a consumer must pass

Nothing in mission-control's channel server was changed. To send an image, the
consumer POSTs to `/send` with the existing `chat_id` and `text` plus **one** of:

- `attachment_path` - an absolute path **on iMac27**, not on the Mac Studio. A
  crop rendered on the Mac Studio is NOT reachable by this field.
- `attachment_b64` + `attachment_name` - the image bytes, base64, plus a file
  name whose extension identifies the image type. **This is the shape a
  mission-control consumer wants**, because the bridge is on a different host.

The TypeScript helper `remote-send.ts` mirrors both shapes as
`options.attachment`: `{ path }` or `{ base64, name }`.

## Tests

- `python3 -m pytest` - 66 passed, 4 skipped (2 optional live-chat.db fixtures
  in each of the two attachment suites).
- `IMESSAGE_TEST_CHATDB=<copy> python3 -m pytest test_attributed_body.py` - adds
  the ground-truth pass: 5,363 rows carrying both `text` and `attributedBody`
  decoded to exactly their `text` column, 0 mismatches, 0 failures; all 274,095
  NULL-text rows decoded.
- `bun test` - 32 passed, 1 skipped.

## Rollback with auth in the picture

Rolling the bridge back to `3ce9c66` removes the auth requirement, so both
un-updated and updated clients work again: the updated ones send a header the
old bridge simply ignores. The token file can stay on disk; it is inert to the
old binary. That is deliberate - it makes rollback safe at any point in the
sequence, which is why the client changes are additive rather than a swap.

## Timing

The ticket already said to deploy while Greg is awake, because this touches the
live channel he depends on. mc-btl9u makes that more important, not less:
between the bridge restart and the last client update, iMessage delivery to
Albert is down. Do the client changes first, then the bridge, and keep the
rollback command in the buffer.
- Cross-decoder check against the canonical TypeScript decoder
  (`mission-control/channels/imessage/attributed-body.ts`) over 274,095 real
  NULL-text rows: 274,095 agree, 0 disagree.
