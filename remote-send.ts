import { bridgeAuthHeaders } from './bridge-auth'

export type FetchImpl = (
  input: string | URL | Request,
  init?: RequestInit,
) => Promise<Response>

/**
 * An optional image to send alongside the text (mc-am50p). Two shapes, matching
 * the bridge's POST /send:
 *
 *   { path }                 an absolute path on the BRIDGE host, not on this
 *                            machine. Only use it when the caller knows the two
 *                            are the same box or share the path.
 *   { base64, name }         the bytes themselves; the bridge stages them in a
 *                            0600 temp file and deletes it after the send. This
 *                            is the shape a remote caller wants.
 */
export type RemoteAttachment =
  | { path: string; base64?: never; name?: never }
  | { base64: string; name: string; path?: never }

export type RemoteSendOptions = {
  attachment?: RemoteAttachment
  /**
   * Shared secret for the X-Bridge-Token header (mc-btl9u). Omit it and the
   * token is read from the file named by IMESSAGE_BRIDGE_TOKEN_FILE. There is
   * no unauthenticated path: a bridge request without the header is a 401.
   */
  token?: string
  attempts?: number
  fetchImpl?: FetchImpl
  probeBeforeSend?: boolean
  retryDelayMs?: number
  timeoutMs?: number
  log?: (message: string) => void
}

/** Build the POST /send body, omitting attachment fields when there is none. */
function sendPayload(
  chatId: string,
  text: string,
  attachment?: RemoteAttachment,
): Record<string, string> {
  const payload: Record<string, string> = { chat_id: chatId, text }
  if (!attachment) return payload
  if (attachment.path !== undefined) {
    payload.attachment_path = attachment.path
  } else {
    payload.attachment_b64 = attachment.base64
    payload.attachment_name = attachment.name
  }
  return payload
}

const DEFAULT_ATTEMPTS = 2
const DEFAULT_RETRY_DELAY_MS = 250
const DEFAULT_TIMEOUT_MS = 5_000

function requestInit(init: RequestInit, timeoutMs: number): RequestInit {
  return {
    ...init,
    signal: AbortSignal.timeout(timeoutMs),
  }
}

function bridgeEndpoint(bridgeUrl: string, path: string): string {
  return `${bridgeUrl.replace(/\/+$/, '')}${path}`
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

async function readBody(res: Response): Promise<string> {
  return res.text().catch(() => '(no body)')
}

function delay(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

export async function probeRemoteBridge(
  bridgeUrl: string,
  options: Pick<RemoteSendOptions, 'fetchImpl' | 'timeoutMs' | 'token'> = {},
): Promise<void> {
  const fetchImpl = options.fetchImpl ?? fetch
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS

  // Any HTTP response proves the TCP/HTTP path is alive. Older bridges or
  // future deployments may not make /info authoritative for send readiness, so
  // keep this probe about connection freshness rather than policy.
  const res = await fetchImpl(
    bridgeEndpoint(bridgeUrl, '/info'),
    requestInit(
      {
        headers: { Accept: 'application/json', ...bridgeAuthHeaders(options.token) },
      },
      timeoutMs,
    ),
  )
  await res.arrayBuffer().catch(() => undefined)
}

export async function postRemoteMessage(
  bridgeUrl: string,
  chatId: string,
  text: string,
  options: Pick<
    RemoteSendOptions,
    'attachment' | 'fetchImpl' | 'timeoutMs' | 'token'
  > = {},
): Promise<void> {
  const fetchImpl = options.fetchImpl ?? fetch
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS

  const res = await fetchImpl(
    bridgeEndpoint(bridgeUrl, '/send'),
    requestInit(
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...bridgeAuthHeaders(options.token),
        },
        body: JSON.stringify(sendPayload(chatId, text, options.attachment)),
      },
      timeoutMs,
    ),
  )

  if (!res.ok) {
    const body = await readBody(res)
    throw new Error(`bridge returned ${res.status}: ${body}`)
  }
}

export async function sendRemoteWithKeepalive(
  bridgeUrl: string,
  chatId: string,
  text: string,
  options: RemoteSendOptions = {},
): Promise<void> {
  const attempts = Math.max(1, options.attempts ?? DEFAULT_ATTEMPTS)
  const probeBeforeSend = options.probeBeforeSend ?? true
  const retryDelayMs = options.retryDelayMs ?? DEFAULT_RETRY_DELAY_MS
  let lastError: unknown = undefined

  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    if (probeBeforeSend) {
      try {
        await probeRemoteBridge(bridgeUrl, options)
      } catch (err) {
        lastError = err
        options.log?.(
          `bridge keepalive failed on attempt ${attempt}/${attempts}: ${errorMessage(err)}`,
        )
        if (attempt < attempts) {
          await delay(retryDelayMs)
          continue
        }
        // Preserve old behavior as a final fallback: even if the best-effort
        // probe fails, try the actual send once before reporting failure.
      }
    }

    try {
      await postRemoteMessage(bridgeUrl, chatId, text, options)
      return
    } catch (err) {
      lastError = err
      options.log?.(
        `bridge send failed on attempt ${attempt}/${attempts}: ${errorMessage(err)}`,
      )
      if (attempt < attempts) {
        await delay(retryDelayMs)
      }
    }
  }

  throw new Error(
    `bridge send failed after ${attempts} attempt(s): ${errorMessage(lastError)}`,
  )
}
