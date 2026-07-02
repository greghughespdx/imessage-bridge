import { describe, expect, test } from 'bun:test'
import { sendRemoteWithKeepalive, type FetchImpl } from './remote-send'

type FetchCall = {
  body?: string
  init?: RequestInit
  url: string
}

function makeFetch(
  handler: (call: FetchCall, index: number) => Response | Error,
): { calls: FetchCall[]; fetchImpl: FetchImpl } {
  const calls: FetchCall[] = []
  const fetchImpl: FetchImpl = async (input, init) => {
    const call = {
      body: typeof init?.body === 'string' ? init.body : undefined,
      init,
      url: String(input),
    }
    calls.push(call)
    const result = handler(call, calls.length - 1)
    if (result instanceof Error) {
      throw result
    }
    return result
  }
  return { calls, fetchImpl }
}

describe('sendRemoteWithKeepalive', () => {
  test('probes bridge before sending a remote message', async () => {
    const { calls, fetchImpl } = makeFetch(call => {
      if (call.url.endsWith('/info')) return new Response('{}', { status: 200 })
      return new Response('{"status":"sent"}', { status: 200 })
    })

    await sendRemoteWithKeepalive('http://bridge.local:8432/', 'chat-id', 'hello', {
      fetchImpl,
      retryDelayMs: 0,
    })

    expect(calls.map(call => call.url)).toEqual([
      'http://bridge.local:8432/info',
      'http://bridge.local:8432/send',
    ])
    expect(calls[1].init?.method).toBe('POST')
    expect(JSON.parse(calls[1].body ?? '{}')).toEqual({
      chat_id: 'chat-id',
      text: 'hello',
    })
  })

  test('retries when the first keepalive probe hits a stale connection', async () => {
    const { calls, fetchImpl } = makeFetch(call => {
      if (calls.length === 1) return new Error('socket hang up')
      if (call.url.endsWith('/info')) return new Response('{}', { status: 200 })
      return new Response('{"status":"sent"}', { status: 200 })
    })

    await sendRemoteWithKeepalive('http://bridge.local:8432', 'chat-id', 'hello', {
      fetchImpl,
      retryDelayMs: 0,
    })

    expect(calls.map(call => call.url)).toEqual([
      'http://bridge.local:8432/info',
      'http://bridge.local:8432/info',
      'http://bridge.local:8432/send',
    ])
  })

  test('retries when the first send hits a stale connection', async () => {
    const { calls, fetchImpl } = makeFetch(call => {
      if (call.url.endsWith('/send') && calls.length === 2) {
        return new Error('connection reset by peer')
      }
      if (call.url.endsWith('/info')) return new Response('{}', { status: 200 })
      return new Response('{"status":"sent"}', { status: 200 })
    })

    await sendRemoteWithKeepalive('http://bridge.local:8432', 'chat-id', 'hello', {
      fetchImpl,
      retryDelayMs: 0,
    })

    expect(calls.map(call => call.url)).toEqual([
      'http://bridge.local:8432/info',
      'http://bridge.local:8432/send',
      'http://bridge.local:8432/info',
      'http://bridge.local:8432/send',
    ])
  })

  test('does not require /info to return 200', async () => {
    const { calls, fetchImpl } = makeFetch(call => {
      if (call.url.endsWith('/info')) return new Response('not found', { status: 404 })
      return new Response('{"status":"sent"}', { status: 200 })
    })

    await sendRemoteWithKeepalive('http://bridge.local:8432', 'chat-id', 'hello', {
      fetchImpl,
      retryDelayMs: 0,
    })

    expect(calls).toHaveLength(2)
    expect(calls[1].url).toBe('http://bridge.local:8432/send')
  })

  test('reports the final bridge response when all send attempts fail', async () => {
    const { fetchImpl } = makeFetch(call => {
      if (call.url.endsWith('/info')) return new Response('{}', { status: 200 })
      return new Response('bad chat', { status: 500 })
    })

    await expect(
      sendRemoteWithKeepalive('http://bridge.local:8432', 'chat-id', 'hello', {
        attempts: 1,
        fetchImpl,
        retryDelayMs: 0,
      }),
    ).rejects.toThrow('bridge send failed after 1 attempt(s): bridge returned 500: bad chat')
  })
})
