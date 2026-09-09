/**
 * End-to-end tests for iMessage image-attachment materialization (mc-iee8).
 *
 * Self-contained: builds a synthetic chat.db + attachments dir, starts the REAL
 * bridge.py against it on a random port, and exercises the remote pipeline
 * (fetch bytes -> save -> HEIC->PNG). No live channel, no live bridge, no live
 * chat.db. A HEIC fixture is generated on the fly with `sips` (png->heic->png).
 *
 * An opt-in block also proves the real 2026-07-12 test image if
 * IMESSAGE_TEST_CHATDB (a read-only chat.db copy) is provided.
 *
 * Run: bun test attachments.test.ts
 */

import { test, expect, beforeAll, afterAll, describe } from 'bun:test'
import { Database } from 'bun:sqlite'
import * as fs from 'fs'
import * as os from 'os'
import * as path from 'path'
import {
  materializeImage,
  fetchAttachmentBytes,
  isImageAttachment,
  pickImageAttachment,
  safeName,
  type AttachmentMeta,
} from './attachments'
import { bridgeAuthHeaders } from './bridge-auth'

const APPLE_EPOCH_OFFSET_S = 978307200
const nowMs = Date.now()
const appleNs = (unixMs: number) =>
  (unixMs / 1000 - APPLE_EPOCH_OFFSET_S) * 1_000_000_000

// Real 1x1 PNG bytes.
const PNG_1X1 = Buffer.from(
  '89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4' +
    '890000000d49444154789c6360000002000100ffff03000006000557bfabd400' +
    '00000049454e44ae426082',
  'hex',
)

const PNG_MAGIC = Buffer.from([0x89, 0x50, 0x4e, 0x47])

// Throwaway value written to a temp file for the spawned bridge (mc-btl9u).
// Never a real token and never read from the developer's own token file.
const TEST_TOKEN = 'test-token-not-a-real-secret'
let tokenFile: string

let tmp: string
let dbPath: string
let attDir: string
let bridgeProc: any
let bridgeUrl: string
let pngAttPath: string
let heicAttPath: string
let heicAvailable = false

function isPng(p: string): boolean {
  const buf = fs.readFileSync(p)
  return buf.subarray(0, 4).equals(PNG_MAGIC)
}

async function waitForBridge(url: string, timeoutMs = 8000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${url}/healthz`, {
        headers: bridgeAuthHeaders(TEST_TOKEN),
        signal: AbortSignal.timeout(1000),
      })
      if (res.ok || res.status === 503) return
    } catch {}
    await new Promise(r => setTimeout(r, 150))
  }
  throw new Error(`bridge did not come up at ${url}`)
}

beforeAll(async () => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'mc-iee8-ts-'))
  attDir = path.join(tmp, 'Attachments')
  const sub = path.join(attDir, 'aa', 'bb', 'ATTGUID')
  fs.mkdirSync(sub, { recursive: true })

  pngAttPath = path.join(sub, 'photo.png')
  fs.writeFileSync(pngAttPath, PNG_1X1)

  // Generate a HEIC fixture from the PNG via sips (self-contained HEIC test).
  heicAttPath = path.join(sub, 'photo.heic')
  try {
    const conv = Bun.spawnSync([
      'sips',
      '-s',
      'format',
      'heic',
      pngAttPath,
      '--out',
      heicAttPath,
    ])
    heicAvailable = conv.exitCode === 0 && fs.existsSync(heicAttPath)
  } catch {
    heicAvailable = false
  }

  // Build synthetic chat.db.
  dbPath = path.join(tmp, 'chat.db')
  const db = new Database(dbPath)
  db.exec(`
    CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
    CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT, style INTEGER, display_name TEXT);
    CREATE TABLE message (ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT,
      attributedBody BLOB, date INTEGER,
      is_from_me INTEGER, cache_has_attachments INTEGER DEFAULT 0, handle_id INTEGER, service TEXT);
    CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
    CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, guid TEXT, filename TEXT, mime_type TEXT,
      transfer_name TEXT, uti TEXT, total_bytes INTEGER);
    CREATE TABLE message_attachment_join (ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
      message_id INTEGER, attachment_id INTEGER);
  `)
  db.run("INSERT INTO handle (ROWID, id) VALUES (1, '+15034102254')")
  db.run(
    "INSERT INTO chat (ROWID, guid, style) VALUES (1, 'iMessage;-;+15034102254', 45)",
  )
  db.run(
    'INSERT INTO message (ROWID, guid, text, date, is_from_me, cache_has_attachments, handle_id, service) ' +
      "VALUES (10, 'GUID-PNG', NULL, ?, 0, 1, 1, 'iMessage')",
    [appleNs(nowMs - 3000)],
  )
  db.run('INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 10)')
  db.run(
    'INSERT INTO attachment (ROWID, guid, filename, mime_type, transfer_name, uti) ' +
      "VALUES (100, 'ATT-PNG', ?, 'image/png', 'photo.png', 'public.png')",
    [pngAttPath],
  )
  db.run(
    'INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (10, 100)',
  )
  if (heicAvailable) {
    db.run(
      'INSERT INTO message (ROWID, guid, text, date, is_from_me, cache_has_attachments, handle_id, service) ' +
        "VALUES (11, 'GUID-HEIC', NULL, ?, 0, 1, 1, 'iMessage')",
      [appleNs(nowMs - 2000)],
    )
    db.run('INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 11)')
    db.run(
      'INSERT INTO attachment (ROWID, guid, filename, mime_type, transfer_name, uti) ' +
        "VALUES (101, 'ATT-HEIC', ?, 'image/heic', 'photo.heic', 'public.heic')",
      [heicAttPath],
    )
    db.run(
      'INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (11, 101)',
    )
  }
  db.close()

  // The bridge refuses to start without a 0600 token file (mc-btl9u).
  tokenFile = path.join(tmp, 'token')
  fs.writeFileSync(tokenFile, TEST_TOKEN + '\n', { mode: 0o600 })
  fs.chmodSync(tokenFile, 0o600)

  // Start the real bridge.py. --bind 127.0.0.1 keeps the test bridge off the
  // LAN; the default would be this machine's LAN address.
  const port = 18400 + Math.floor(Math.random() * 900)
  bridgeUrl = `http://127.0.0.1:${port}`
  bridgeProc = Bun.spawn(
    [
      'python3',
      path.join(import.meta.dir, 'bridge.py'),
      '--port',
      String(port),
      '--db',
      dbPath,
      '--no-bonjour',
      '--bind',
      '127.0.0.1',
    ],
    {
      env: {
        ...process.env,
        IMESSAGE_ATTACHMENTS_DIR: attDir,
        IMESSAGE_BRIDGE_LOG: path.join(tmp, 'bridge.log'),
        IMESSAGE_BRIDGE_TOKEN_FILE: tokenFile,
      },
      stdout: 'ignore',
      stderr: 'ignore',
    },
  )
  await waitForBridge(bridgeUrl)
})

afterAll(() => {
  try {
    bridgeProc?.kill()
  } catch {}
})

describe('pure helpers', () => {
  test('isImageAttachment by mime and uti', () => {
    expect(isImageAttachment('image/png', null)).toBe(true)
    expect(isImageAttachment(null, 'public.heic')).toBe(true)
    expect(isImageAttachment('application/pdf', 'com.adobe.pdf')).toBe(false)
    expect(isImageAttachment(null, null)).toBe(false)
  })

  test('pickImageAttachment picks first image', () => {
    const atts: AttachmentMeta[] = [
      { index: 0, mime_type: 'application/pdf', transfer_name: 'a.pdf', is_image: false },
      { index: 1, mime_type: 'image/jpeg', transfer_name: 'b.jpg', is_image: true },
    ]
    expect(pickImageAttachment(atts)?.index).toBe(1)
  })

  test('safeName strips path + unsafe chars', () => {
    expect(safeName('../../etc/passwd', 'fallback')).toBe('passwd')
    expect(safeName('a b*c.png', 'fallback')).toBe('a_b_c.png')
    expect(safeName(null, 'fallback')).toBe('fallback')
  })
})

describe('bridge /messages + /attachment', () => {
  test('image-only message surfaces with attachment metadata', async () => {
    const res = await fetch(`${bridgeUrl}/messages?after=0`, {
      headers: bridgeAuthHeaders(TEST_TOKEN),
    })
    expect(res.ok).toBe(true)
    const msgs = (await res.json()) as any[]
    const m = msgs.find(x => x.guid === 'GUID-PNG')
    expect(m).toBeDefined()
    expect(m.text).toBeNull()
    expect(m.attachments).toHaveLength(1)
    expect(m.attachments[0].is_image).toBe(true)
    expect(m.attachments[0].mime_type).toBe('image/png')
  })

  test('fetchAttachmentBytes returns the exact file bytes', async () => {
    const bytes = await fetchAttachmentBytes(
      bridgeUrl, 'GUID-PNG', 0, undefined, TEST_TOKEN,
    )
    expect(bytes).not.toBeNull()
    expect(Buffer.from(bytes!).equals(PNG_1X1)).toBe(true)
  })

  test('fetchAttachmentBytes returns null for unknown message', async () => {
    expect(
      await fetchAttachmentBytes(bridgeUrl, 'NOPE', 0, undefined, TEST_TOKEN),
    ).toBeNull()
  })
})

describe('materializeImage (remote)', () => {
  test('png attachment lands in cache as-is', async () => {
    const cacheDir = path.join(tmp, 'cache-png')
    const out = await materializeImage({
      guid: 'GUID-PNG',
      attachment: {
        index: 0,
        mime_type: 'image/png',
        transfer_name: 'photo.png',
        is_image: true,
      },
      source: { kind: 'remote', bridgeUrl, token: TEST_TOKEN },
      cacheDir,
    })
    expect(out).toBeDefined()
    expect(fs.existsSync(out!)).toBe(true)
    expect(isPng(out!)).toBe(true)
  })

  test('HEIC attachment is converted to PNG', async () => {
    if (!heicAvailable) return // sips unavailable; covered by real-fixture block
    const cacheDir = path.join(tmp, 'cache-heic')
    const out = await materializeImage({
      guid: 'GUID-HEIC',
      attachment: {
        index: 0,
        mime_type: 'image/heic',
        transfer_name: 'photo.heic',
        is_image: true,
      },
      source: { kind: 'remote', bridgeUrl, token: TEST_TOKEN },
      cacheDir,
    })
    expect(out).toBeDefined()
    expect(out!.endsWith('.png')).toBe(true)
    expect(isPng(out!)).toBe(true)
  })
})

describe('materializeImage (local)', () => {
  test('reads a local file directly', async () => {
    const cacheDir = path.join(tmp, 'cache-local')
    const out = await materializeImage({
      guid: 'GUID-LOCAL',
      attachment: {
        index: 0,
        mime_type: 'image/png',
        transfer_name: 'photo.png',
        is_image: true,
        localPath: pngAttPath,
      },
      source: { kind: 'local' },
      cacheDir,
    })
    expect(out).toBeDefined()
    expect(isPng(out!)).toBe(true)
  })
})

// Opt-in: prove the real 2026-07-12 IMG_3623.HEIC end to end. Requires a
// read-only chat.db copy and the real attachments dir on disk.
describe('real fixture (opt-in)', () => {
  const testDb = process.env.IMESSAGE_TEST_CHATDB
  const FIXTURE_GUID = '7AF7B37F-10CB-403E-B936-4920EE367758'

  test.if(!!testDb)('extracts + converts the real test image to PNG', async () => {
    const port = 19300 + Math.floor(Math.random() * 600)
    const url = `http://127.0.0.1:${port}`
    const proc = Bun.spawn(
      ['python3', path.join(import.meta.dir, 'bridge.py'), '--port', String(port),
        '--db', testDb!, '--no-bonjour', '--bind', '127.0.0.1'],
      {
        env: { ...process.env, IMESSAGE_BRIDGE_TOKEN_FILE: tokenFile },
        stdout: 'ignore',
        stderr: 'ignore',
      },
    )
    try {
      await waitForBridge(url)
      const outDir = '/tmp/mc-iee8-scratch'
      fs.mkdirSync(outDir, { recursive: true })
      const out = await materializeImage({
        guid: FIXTURE_GUID,
        attachment: {
          index: 0,
          mime_type: 'image/heic',
          transfer_name: 'IMG_3623.HEIC',
          is_image: true,
        },
        source: { kind: 'remote', bridgeUrl: url, token: TEST_TOKEN },
        cacheDir: outDir,
      })
      expect(out).toBeDefined()
      expect(out!.endsWith('.png')).toBe(true)
      expect(isPng(out!)).toBe(true)
      // Leave a copy at a stable path for the orchestrator to Read as proof.
      fs.copyFileSync(out!, path.join(outDir, 'PROOF-real-fixture.png'))
      process.stderr.write(`\nREAL FIXTURE PROOF PNG: ${path.join(outDir, 'PROOF-real-fixture.png')}\n`)
    } finally {
      proc.kill()
    }
  })
})
