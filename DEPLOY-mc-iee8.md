# Deploy: iMessage image attachments (mc-iee8)

Adds image-attachment delivery to the iMessage channel. An inbound iMessage with
an image now surfaces `image_path` in the channel event meta (like Telegram),
pointing at a local, harness-readable file (HEIC converted to PNG).

## What changed

Two sides, both in this repo (canonical source):

- **`bridge.py`** (runs on iMac27, `192.168.15.12:8432`): `/messages` now
  surfaces image-only messages (previously dropped by `WHERE text IS NOT NULL`)
  and returns per-message `attachments` metadata. New `GET /attachment?msg=<guid>&index=<n>`
  serves raw image bytes (read-only, path-traversal-guarded, images only, 50MB cap).
- **`channel-server.ts`** (upstream template) / the deployed live channel: fetches
  the first image attachment, caches it under `~/.claude/image-cache/imessage/`,
  converts HEIC->PNG via `sips`, sets `meta.image_path`. New shared helper module
  **`attachments.ts`** holds the materialization logic (no new npm deps; Bun +
  node builtins only).

## Live deployment map

The live channel is NOT this repo's `channel-server.ts`. It is a diverged copy at
`~/Dev/mission-control/channels/imessage/server.ts` (adds a sender allowlist +
logging). Ported, ready-to-copy live files are staged at `/tmp/mc-iee8-staged/`
(`server.ts`, `attachments.ts`) with the allowlist preserved.

| Component | Runs on | Live path |
|-----------|---------|-----------|
| Bridge (`bridge.py`) | iMac27 | (iMac27) `~/Library/.../imessage-bridge` service, `:8432` |
| Channel (`server.ts` + `attachments.ts`) | Mac Studio | `~/Dev/mission-control/channels/imessage/` (launched by Claude Code `.mcp.json`) |

## Deployment ORDER (matters)

Deploy the **channel first**, then the bridge. Rationale: the new bridge surfaces
image-only messages with `text = NULL`; the OLD channel does `msg.text.slice(...)`
and would crash on null. The new channel maps `text ?? ''` and is safe. New
channel + old bridge is also safe (no `attachments` field -> no image, text
unaffected).

### 1. Channel (Mac Studio) - watched restart

```
cp /tmp/mc-iee8-staged/attachments.ts ~/Dev/mission-control/channels/imessage/attachments.ts
cp /tmp/mc-iee8-staged/server.ts      ~/Dev/mission-control/channels/imessage/server.ts
# reload the MCP channel (orchestrator session reload / restart the imessage MCP worker)
```

Rollback: `git checkout e1898c1 -- channels/imessage/server.ts && rm channels/imessage/attachments.ts` then reload.

### 2. Bridge (iMac27)

Deploy the new `bridge.py` to the iMac27 bridge service and restart it. (SSH as
`greg@192.168.15.12` was refused during build - confirm the working deploy path /
user for that host.) Backward compatible: only adds an endpoint + a field.

Rollback: redeploy the v0.2.0 `bridge.py` (repo `master`, commit `68b5b2b`) and restart.

## Post-restart verification

1. `curl -s http://192.168.15.12:8432/healthz` -> `status: ok`.
2. Greg resends the two walkthrough screengrabs (and/or the IMG_3623 test image).
3. Confirm the channel event carries `image_path` and the session can Read the
   image (HEIC arrives as a `.png` under `~/.claude/image-cache/imessage/`).
4. Confirm plain text messages still deliver unchanged.

## Tests

- `python3 test_attachments.py` - 13 tests (bridge; synthetic + live HTTP).
- `bun test attachments.test.ts` - 10 tests (channel; real bridge.py subprocess
  + synthetic chat.db + sips HEIC round-trip).
- Opt-in real fixture (Greg's 2026-07-12 test image): set
  `IMESSAGE_TEST_CHATDB=<read-only chat.db copy>` for either suite.
