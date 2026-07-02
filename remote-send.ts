export type FetchImpl = (
  input: string | URL | Request,
  init?: RequestInit,
) => Promise<Response>

export type RemoteSendOptions = {
  attempts?: number
  fetchImpl?: FetchImpl
  probeBeforeSend?: boolean
  retryDelayMs?: number
  timeoutMs?: number
  log?: (message: string) => void
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
  options: Pick<RemoteSendOptions, 'fetchImpl' | 'timeoutMs'> = {},
): Promise<void> {
  const fetchImpl = options.fetchImpl ?? fetch
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS

  // Any HTTP response proves the TCP/HTTP path is alive. Older bridges or
  // future deployments may not make /info authoritative for send readiness, so
  // keep this probe about connection freshness rather than policy.
  const res = await fetchImpl(
    bridgeEndpoint(bridgeUrl, '/info'),
    requestInit({ headers: { Accept: 'application/json' } }, timeoutMs),
  )
  await res.arrayBuffer().catch(() => undefined)
}

export async function postRemoteMessage(
  bridgeUrl: string,
  chatId: string,
  text: string,
  options: Pick<RemoteSendOptions, 'fetchImpl' | 'timeoutMs'> = {},
): Promise<void> {
  const fetchImpl = options.fetchImpl ?? fetch
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS

  const res = await fetchImpl(
    bridgeEndpoint(bridgeUrl, '/send'),
    requestInit(
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chat_id: chatId, text }),
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
