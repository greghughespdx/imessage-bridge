/**
 * Tests for the shared bridge token reader (mc-btl9u).
 *
 * Every token here is a throwaway literal written to a temp file. No test
 * reads, writes, or prints a real token.
 */

import { describe, expect, test } from 'bun:test'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'
import {
  BRIDGE_TOKEN_HEADER,
  BridgeTokenError,
  bridgeAuthHeaders,
  bridgeTokenFilePath,
  readBridgeToken,
} from './bridge-auth'

const TEST_TOKEN = 'test-token-not-a-real-secret'

function tokenFile(content: string, mode = 0o600): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'mc-btl9u-ts-'))
  const file = path.join(dir, 'token')
  fs.writeFileSync(file, content)
  fs.chmodSync(file, mode)
  return file
}

describe('bridgeTokenFilePath', () => {
  test('IMESSAGE_BRIDGE_TOKEN_FILE wins', () => {
    expect(
      bridgeTokenFilePath({ IMESSAGE_BRIDGE_TOKEN_FILE: '/somewhere/token' }),
    ).toBe('/somewhere/token')
  })

  test('defaults under the home directory', () => {
    expect(bridgeTokenFilePath({})).toBe(
      path.join(os.homedir(), '.config/imessage-bridge/token'),
    )
  })

  test('expands a leading tilde in the override', () => {
    expect(
      bridgeTokenFilePath({ IMESSAGE_BRIDGE_TOKEN_FILE: '~/elsewhere/token' }),
    ).toBe(path.join(os.homedir(), 'elsewhere/token'))
  })
})

describe('readBridgeToken', () => {
  test('reads a 0600 file and strips the trailing newline', () => {
    expect(readBridgeToken(tokenFile(TEST_TOKEN + '\n'))).toBe(TEST_TOKEN)
  })

  test('throws on a missing file', () => {
    expect(() => readBridgeToken('/nope/definitely/not/here')).toThrow(
      BridgeTokenError,
    )
  })

  test('throws on a group or world accessible file', () => {
    expect(() => readBridgeToken(tokenFile(TEST_TOKEN, 0o640))).toThrow(
      /group or world accessible/,
    )
    expect(() => readBridgeToken(tokenFile(TEST_TOKEN, 0o604))).toThrow(
      /group or world accessible/,
    )
  })

  test('throws on an empty file', () => {
    expect(() => readBridgeToken(tokenFile('  \n'))).toThrow(/is empty/)
  })

  test('no thrown message contains the token', () => {
    try {
      readBridgeToken(tokenFile(TEST_TOKEN, 0o644))
      throw new Error('expected a BridgeTokenError')
    } catch (err) {
      expect(String(err)).not.toContain(TEST_TOKEN)
    }
  })
})

describe('bridgeAuthHeaders', () => {
  test('uses an explicit token without touching the filesystem', () => {
    expect(bridgeAuthHeaders(TEST_TOKEN)).toEqual({
      [BRIDGE_TOKEN_HEADER]: TEST_TOKEN,
    })
  })

  test('header name is exactly X-Bridge-Token', () => {
    expect(BRIDGE_TOKEN_HEADER).toBe('X-Bridge-Token')
  })

  test('fails closed when no token is available', () => {
    const saved = process.env.IMESSAGE_BRIDGE_TOKEN_FILE
    process.env.IMESSAGE_BRIDGE_TOKEN_FILE = '/nope/definitely/not/here'
    try {
      // No silent fallback to an unauthenticated request.
      expect(() => bridgeAuthHeaders()).toThrow(BridgeTokenError)
    } finally {
      if (saved === undefined) delete process.env.IMESSAGE_BRIDGE_TOKEN_FILE
      else process.env.IMESSAGE_BRIDGE_TOKEN_FILE = saved
    }
  })
})
