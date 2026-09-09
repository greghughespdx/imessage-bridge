/**
 * Shared-secret auth for every bridge client (mc-btl9u).
 *
 * The bridge requires an X-Bridge-Token header on every route. The secret
 * lives in a 0600 file on both ends; this module is the one place any client
 * in this repo reads it, so there is a single answer to "where does the token
 * come from" and no route can quietly skip it.
 *
 * The file is read on each call rather than cached. It is a few bytes of local
 * disk, and reading it fresh means rotating the secret takes effect without
 * restarting anything that holds a long-lived poll loop.
 */

import { readFileSync, statSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

export const BRIDGE_TOKEN_HEADER = 'X-Bridge-Token'
export const DEFAULT_TOKEN_FILE = '~/.config/imessage-bridge/token'

export class BridgeTokenError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'BridgeTokenError'
  }
}

function expandHome(p: string): string {
  if (p === '~') return homedir()
  if (p.startsWith('~/')) return join(homedir(), p.slice(2))
  return p
}

/** Absolute path of the token file. IMESSAGE_BRIDGE_TOKEN_FILE overrides. */
export function bridgeTokenFilePath(
  env: Record<string, string | undefined> = process.env,
): string {
  return expandHome(env.IMESSAGE_BRIDGE_TOKEN_FILE || DEFAULT_TOKEN_FILE)
}

/**
 * Read the shared secret. Throws BridgeTokenError when the file is missing,
 * group/world accessible, or empty - the same three refusals the bridge makes,
 * so a misconfigured client fails at the same boundary as a misconfigured
 * server. The token value never appears in any thrown message.
 */
export function readBridgeToken(tokenFile?: string): string {
  const path = tokenFile ? expandHome(tokenFile) : bridgeTokenFilePath()
  let mode: number
  try {
    mode = statSync(path).mode
  } catch (err) {
    throw new BridgeTokenError(
      `cannot read the bridge token file ${path}: ${
        err instanceof Error ? err.message : String(err)
      }. Create it with \`install -m 600 /dev/null ${path}\` and write the ` +
        'shared secret into it, or set IMESSAGE_BRIDGE_TOKEN_FILE.',
    )
  }
  if (mode & 0o077) {
    throw new BridgeTokenError(
      `bridge token file ${path} is group or world accessible ` +
        `(mode ${(mode & 0o777).toString(8)}). Run \`chmod 600 ${path}\`.`,
    )
  }
  const token = readFileSync(path, 'utf8').trim()
  if (!token) throw new BridgeTokenError(`bridge token file ${path} is empty`)
  return token
}

/**
 * Headers for a bridge request. Pass an explicit token to skip the file read
 * (tests do this with a throwaway value); otherwise the file is the source.
 */
export function bridgeAuthHeaders(token?: string): Record<string, string> {
  return { [BRIDGE_TOKEN_HEADER]: token ?? readBridgeToken() }
}
