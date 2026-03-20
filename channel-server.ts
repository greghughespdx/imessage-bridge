#!/usr/bin/env bun
/**
 * iMessage channel for Claude Code.
 *
 * Supports two modes:
 *   - Local: reads ~/Library/Messages/chat.db directly via bun:sqlite,
 *            sends via osascript.
 *   - Remote: polls a bridge HTTP server (bridge.py) for messages,
 *             sends through it.
 *
 * Mode is selected at startup:
 *   1. If IMESSAGE_BRIDGE_URL is set -> remote mode (no discovery).
 *   2. Otherwise try to open chat.db -> local mode.
 *   3. Try Bonjour discovery (_imessage-bridge._tcp) -> remote mode.
 *   4. Nothing works -> log error and exit.
 */

import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import {
  ListToolsRequestSchema,
  CallToolRequestSchema,
} from '@modelcontextprotocol/sdk/types.js'
import { Database } from 'bun:sqlite'
import * as os from 'os'
import * as path from 'path'

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const POLL_INTERVAL_MS = 1_000

// Apple epoch offset: seconds between 1970-01-01 and 2001-01-01
const APPLE_EPOCH_OFFSET_S = 978307200

// chat.db stores dates in nanoseconds since Apple epoch.
// To convert lastSeenTs (unix ms) to Apple nanoseconds:
//   (unix_ms / 1000 - APPLE_EPOCH_OFFSET_S) * 1e9
// To convert Apple nanoseconds to unix ms:
//   (apple_ns / 1e9 + APPLE_EPOCH_OFFSET_S) * 1000

const DB_PATH = path.join(os.homedir(), 'Library', 'Messages', 'chat.db')

const QUERY = `
  SELECT m.guid,
         m.text,
         m.date,
         m.is_from_me,
         h.id        AS sender,
         c.guid      AS chat_guid
  FROM   message m
  LEFT JOIN handle h           ON m.handle_id = h.ROWID
  LEFT JOIN chat_message_join cmj ON m.ROWID  = cmj.message_id
  LEFT JOIN chat c             ON cmj.chat_id = c.ROWID
  WHERE  m.date > ? AND m.text IS NOT NULL AND m.is_from_me = 0
  ORDER  BY m.date ASC
`

// ---------------------------------------------------------------------------
// Bonjour discovery
// ---------------------------------------------------------------------------

/**
 * Run a dns-sd command, read stdout line by line, call onLine() for each.
 * Kills the process and resolves when onLine() returns a non-null value,
 * or after timeoutMs milliseconds (resolves undefined).
 */
async function dnsSd<T>(
  args: string[],
  timeoutMs: number,
  onLine: (line: string) => T | null,
): Promise<T | undefined> {
  const proc = Bun.spawn(['dns-sd', ...args], {
    stdout: 'pipe',
    stderr: 'ignore',
  })

  return new Promise<T | undefined>(resolve => {
    const timer = setTimeout(() => {
      proc.kill()
      resolve(undefined)
    }, timeoutMs)

    // Read stdout line-by-line via async iteration of the ReadableStream.
    ;(async () => {
      const decoder = new TextDecoder()
      let buffer = ''

      try {
        for await (const chunk of proc.stdout) {
          buffer += decoder.decode(chunk, { stream: true })
          const lines = buffer.split('\n')
          buffer = lines.pop() ?? ''

          for (const line of lines) {
            const result = onLine(line)
            if (result !== null) {
              clearTimeout(timer)
              proc.kill()
              resolve(result)
              return
            }
          }
        }
      } catch {
        // Process was killed; ignore stream errors.
      }

      clearTimeout(timer)
      resolve(undefined)
    })()
  })
}

/**
 * Discover an imessage-bridge via Bonjour (_imessage-bridge._tcp local).
 * Returns a bridge URL string if found, or null if nothing is discovered.
 * If IMESSAGE_BRIDGE_NAME is set, only connects to that bridge name.
 */
async function discoverViaBonjour(): Promise<string | null> {
  const wantName = process.env.IMESSAGE_BRIDGE_NAME ?? null

  process.stderr.write('imessage: Bonjour discovery starting...\n')

  // Step 1: Browse for _imessage-bridge._tcp services (3-second timeout).
  // "Add" lines look like:
  //   DATE: ...  Add  ...  local.  _imessage-bridge._tcp.  Albert
  const serviceName = await dnsSd(
    ['-B', '_imessage-bridge._tcp', 'local'],
    3000,
    line => {
      if (!line.includes('Add')) return null
      const parts = line.trim().split(/\s+/)
      const name = parts[parts.length - 1]
      if (!name || name === '_imessage-bridge._tcp.') return null
      if (wantName && name !== wantName) return null
      return name
    },
  )

  if (!serviceName) {
    process.stderr.write(
      wantName
        ? `imessage: Bonjour: bridge "${wantName}" not found\n`
        : 'imessage: Bonjour: no bridges found\n',
    )
    return null
  }

  process.stderr.write(`imessage: Bonjour: found service "${serviceName}"\n`)

  // Step 2: Look up hostname and port (2-second timeout).
  // "can be reached at" lines look like:
  //   DATE: ...  Albert._imessage-bridge._tcp.local. can be reached at iMac27.local.:8432
  const hostPort = await dnsSd(
    ['-L', serviceName, '_imessage-bridge._tcp', 'local'],
    2000,
    line => {
      const m = line.match(/can be reached at ([^:]+):(\d+)/)
      if (!m) return null
      return { hostname: m[1], port: parseInt(m[2], 10) }
    },
  )

  if (!hostPort) {
    process.stderr.write(
      `imessage: Bonjour: could not resolve service "${serviceName}"\n`,
    )
    return null
  }

  const { hostname, port } = hostPort
  process.stderr.write(
    `imessage: Bonjour: "${serviceName}" -> ${hostname}:${port}\n`,
  )

  // Step 3: Resolve hostname to IPv4 address (2-second timeout).
  // "Add" lines look like:
  //   DATE: ...  Add  ...  iMac27.local.  192.168.15.12
  const ip = await dnsSd(['-G', 'v4', hostname], 2000, line => {
    if (!line.includes('Add')) return null
    const parts = line.trim().split(/\s+/)
    const addr = parts[parts.length - 1]
    // Validate it looks like an IPv4 address.
    if (!/^\d{1,3}(\.\d{1,3}){3}$/.test(addr)) return null
    return addr
  })

  if (!ip) {
    // Fall back to mDNS hostname directly.
    process.stderr.write(
      `imessage: Bonjour: could not resolve IP for ${hostname}, using hostname directly\n`,
    )
    return `http://${hostname}:${port}`
  }

  return `http://${ip}:${port}`
}

// ---------------------------------------------------------------------------
// Remote bridge info
// ---------------------------------------------------------------------------

/** Call GET /info on a remote bridge and log the result. */
async function logBridgeInfo(bridgeUrl: string): Promise<void> {
  try {
    const res = await fetch(`${bridgeUrl}/info`, {
      headers: { Accept: 'application/json' },
      signal: AbortSignal.timeout(3000),
    })
    if (res.ok) {
      const info = (await res.json()) as Record<string, unknown>
      const name = info.name ?? info.bridge_name ?? '(unknown)'
      const host = info.hostname ?? info.host ?? '(unknown)'
      process.stderr.write(
        `imessage: bridge connected: name="${name}" host="${host}"\n`,
      )
    } else {
      process.stderr.write(
        `imessage: bridge /info returned ${res.status} (continuing anyway)\n`,
      )
    }
  } catch (err) {
    process.stderr.write(
      `imessage: bridge /info failed: ${err} (continuing anyway)\n`,
    )
  }
}

// ---------------------------------------------------------------------------
// Mode detection
// ---------------------------------------------------------------------------

type Mode =
  | { kind: 'remote'; bridgeUrl: string }
  | { kind: 'local'; db: Database }

async function detectMode(): Promise<Mode> {
  // 1. Explicit bridge URL via env var -> use directly, no discovery.
  const bridgeUrl = process.env.IMESSAGE_BRIDGE_URL
  if (bridgeUrl) {
    process.stderr.write(`imessage: remote mode -> ${bridgeUrl}\n`)
    await logBridgeInfo(bridgeUrl)
    return { kind: 'remote', bridgeUrl }
  }

  // 2. Try local chat.db -> local mode.
  try {
    const db = new Database(DB_PATH, { readonly: true })
    process.stderr.write(`imessage: local mode -> ${DB_PATH}\n`)
    return { kind: 'local', db }
  } catch {
    // Not available locally; continue to Bonjour.
  }

  // 3. Try Bonjour discovery -> remote mode with discovered URL.
  const discovered = await discoverViaBonjour()
  if (discovered) {
    process.stderr.write(`imessage: remote mode (Bonjour) -> ${discovered}\n`)
    await logBridgeInfo(discovered)
    return { kind: 'remote', bridgeUrl: discovered }
  }

  // 4. Nothing worked.
  process.stderr.write(
    `imessage: FATAL: cannot open ${DB_PATH}, IMESSAGE_BRIDGE_URL is not set, ` +
      `and Bonjour discovery found no bridges.\n` +
      `imessage: Grant Full Disk Access to Bun/Terminal, set IMESSAGE_BRIDGE_URL, ` +
      `or run a bridge on the local network advertising _imessage-bridge._tcp.\n`,
  )
  process.exit(1)
}

const mode = await detectMode()

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

// Deduplication: track GUIDs we have already delivered.
const delivered = new Set<string>()

// Start from "now minus one poll interval" so we don't flood on startup.
let lastSeenTs = Date.now() - POLL_INTERVAL_MS

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Escape a string for safe embedding inside an AppleScript quoted string. */
function escapeAppleScript(s: string): string {
  // Backslashes must come first, then double quotes.
  return s.replace(/\\/g, '\\\\').replace(/"/g, '\\"')
}

// ---------------------------------------------------------------------------
// Send implementations
// ---------------------------------------------------------------------------

async function sendLocal(chatId: string, text: string): Promise<void> {
  const escapedText = escapeAppleScript(text)
  const escapedChatId = escapeAppleScript(chatId)
  const script =
    `tell application "Messages" to send "${escapedText}" to chat id "${escapedChatId}"`

  const proc = Bun.spawn(['osascript', '-e', script], {
    stdout: 'pipe',
    stderr: 'pipe',
  })

  const exitCode = await proc.exited

  if (exitCode !== 0) {
    const errText = await new Response(proc.stderr).text().catch(() => '(no stderr)')
    throw new Error(`osascript exited ${exitCode}: ${errText.trim()}`)
  }
}

async function sendRemote(
  bridgeUrl: string,
  chatId: string,
  text: string,
): Promise<void> {
  const res = await fetch(`${bridgeUrl}/send`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chat_id: chatId, text }),
  })

  if (!res.ok) {
    const body = await res.text().catch(() => '(no body)')
    throw new Error(`bridge returned ${res.status}: ${body}`)
  }
}

// ---------------------------------------------------------------------------
// Poll implementations
// ---------------------------------------------------------------------------

type IMessage = {
  guid: string
  text: string
  date_unix_ms: number
  sender: string | null
  chat_guid: string | null
}

function pollLocal(db: Database): IMessage[] {
  const afterAppleNs = (lastSeenTs / 1000 - APPLE_EPOCH_OFFSET_S) * 1_000_000_000

  const rows = db.query(QUERY).all(afterAppleNs) as Array<{
    guid: string
    text: string
    date: number
    is_from_me: number
    sender: string | null
    chat_guid: string | null
  }>

  return rows.map(row => ({
    guid: row.guid,
    text: row.text,
    date_unix_ms: (row.date / 1_000_000_000 + APPLE_EPOCH_OFFSET_S) * 1000,
    sender: row.sender,
    chat_guid: row.chat_guid,
  }))
}

async function pollRemote(bridgeUrl: string): Promise<IMessage[]> {
  const res = await fetch(`${bridgeUrl}/messages?after=${lastSeenTs}`, {
    headers: { Accept: 'application/json' },
  })

  if (!res.ok) {
    const body = await res.text().catch(() => '(no body)')
    throw new Error(`bridge poll returned ${res.status}: ${body}`)
  }

  const data = await res.json()

  if (!Array.isArray(data)) {
    throw new Error(`bridge poll returned non-array: ${JSON.stringify(data)}`)
  }

  return (
    data as Array<{
      guid: string
      text: string
      date: number // unix ms (bridge already converts)
      is_from_me: boolean
      sender: string | null
      chat_guid: string | null
    }>
  )
    .filter(row => !row.is_from_me)
    .map(row => ({
      guid: row.guid,
      text: row.text,
      date_unix_ms: row.date,
      sender: row.sender,
      chat_guid: row.chat_guid,
    }))
}

// ---------------------------------------------------------------------------
// MCP server
// ---------------------------------------------------------------------------

const mcp = new Server(
  { name: 'imessage', version: '0.1.0' },
  {
    capabilities: { tools: {}, experimental: { 'claude/channel': {} } },
    instructions: [
      'iMessages arrive as <channel source="imessage" chat_id="..." message_id="..." from="..." ts="..."> events.',
      'Each event carries meta fields: chat_id (iMessage chat GUID, e.g. "iMessage;-;+15034102254"),',
      'message_id (message GUID for reference), from (sender phone/email), and ts (ISO timestamp).',
      '',
      'When you receive an iMessage, read it and respond using the reply tool.',
      'The reply tool requires chat_id (from the meta) and the text you want to send.',
      'Always respond promptly — the sender is waiting on their phone.',
    ].join('\n'),
  },
)

mcp.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: 'reply',
      description:
        'Send an iMessage reply. Provide the chat_id (iMessage chat GUID) and the text to send.',
      inputSchema: {
        type: 'object',
        properties: {
          chat_id: {
            type: 'string',
            description:
              'iMessage chat GUID (e.g. "iMessage;-;+15034102254"). Copy from the message meta.',
          },
          text: {
            type: 'string',
            description: 'The message text to send.',
          },
        },
        required: ['chat_id', 'text'],
      },
    },
  ],
}))

mcp.setRequestHandler(CallToolRequestSchema, async req => {
  const args = (req.params.arguments ?? {}) as Record<string, unknown>

  if (req.params.name !== 'reply') {
    return {
      content: [{ type: 'text', text: `unknown tool: ${req.params.name}` }],
      isError: true,
    }
  }

  const chatId = args.chat_id as string | undefined
  const text = args.text as string | undefined

  if (!chatId || !text) {
    return {
      content: [{ type: 'text', text: 'reply requires chat_id and text' }],
      isError: true,
    }
  }

  try {
    if (mode.kind === 'local') {
      await sendLocal(chatId, text)
    } else {
      await sendRemote(mode.bridgeUrl, chatId, text)
    }
    return { content: [{ type: 'text', text: `sent to ${chatId}` }] }
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err)
    process.stderr.write(`imessage: send failed for chat_id=${chatId}: ${msg}\n`)
    return {
      content: [{ type: 'text', text: `reply failed: ${msg}` }],
      isError: true,
    }
  }
})

// ---------------------------------------------------------------------------
// Poll loop
// ---------------------------------------------------------------------------

async function poll(): Promise<void> {
  let messages: IMessage[]

  try {
    if (mode.kind === 'local') {
      messages = pollLocal(mode.db)
    } else {
      messages = await pollRemote(mode.bridgeUrl)
    }
  } catch (err) {
    process.stderr.write(`imessage: poll error: ${err}\n`)
    return
  }

  if (messages.length === 0) return

  process.stderr.write(`imessage: ${messages.length} new message(s)\n`)

  for (const msg of messages) {
    if (delivered.has(msg.guid)) continue

    // Update lastSeenTs so subsequent polls don't re-fetch this time range.
    if (msg.date_unix_ms > lastSeenTs) {
      lastSeenTs = msg.date_unix_ms
    }

    mcp
      .notification({
        method: 'notifications/claude/channel',
        params: {
          content: msg.text,
          meta: {
            chat_id: msg.chat_guid ?? '',
            message_id: msg.guid,
            from: msg.sender ?? 'unknown',
            ts: new Date(msg.date_unix_ms).toISOString(),
          },
        },
      })
      .catch(err => {
        process.stderr.write(
          `imessage: notification failed for ${msg.guid}: ${err}\n`,
        )
      })

    delivered.add(msg.guid)
  }
}

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

await mcp.connect(new StdioServerTransport())

process.stderr.write(
  `imessage: polling every ${POLL_INTERVAL_MS / 1000}s ` +
    `(mode: ${mode.kind})\n`,
)

void poll()
setInterval(() => void poll(), POLL_INTERVAL_MS)
