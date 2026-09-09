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

  test('drains the keepalive response before sending', async () => {
    let drained = false
    const { fetchImpl } = makeFetch(call => {
      if (call.url.endsWith('/info')) {
        const res = new Response('bridge diagnostic body', { status: 404 })
        res.arrayBuffer = async () => {
          drained = true
          return new TextEncoder().encode('bridge diagnostic body').buffer
        }
        return res
      }
      expect(drained).toBe(true)
      return new Response('{"status":"sent"}', { status: 200 })
    })

    await sendRemoteWithKeepalive('http://bridge.local:8432', 'chat-id', 'hello', {
      fetchImpl,
      retryDelayMs: 0,
    })
  })

  test('sends a staged image as attachment_b64 plus attachment_name', async () => {
    const { calls, fetchImpl } = makeFetch(call => {
      if (call.url.endsWith('/info')) return new Response('{}', { status: 200 })
      return new Response('{"status":"sent","attachment_sent":true}', { status: 200 })
    })

    await sendRemoteWithKeepalive('http://bridge.local:8432', 'chat-id', 'the mark', {
      attachment: { base64: 'aW1hZ2UtYnl0ZXM=', name: 'crop.png' },
      fetchImpl,
      retryDelayMs: 0,
    })

    expect(JSON.parse(calls[1].body ?? '{}')).toEqual({
      chat_id: 'chat-id',
      text: 'the mark',
      attachment_b64: 'aW1hZ2UtYnl0ZXM=',
      attachment_name: 'crop.png',
    })
  })

  test('sends a bridge-host path as attachment_path', async () => {
    const { calls, fetchImpl } = makeFetch(call => {
      if (call.url.endsWith('/info')) return new Response('{}', { status: 200 })
      return new Response('{"status":"sent","attachment_sent":true}', { status: 200 })
    })

    await sendRemoteWithKeepalive('http://bridge.local:8432', 'chat-id', '', {
      attachment: { path: '/Users/someone/crop.png' },
      fetchImpl,
      retryDelayMs: 0,
    })

    expect(JSON.parse(calls[1].body ?? '{}')).toEqual({
      chat_id: 'chat-id',
      text: '',
      attachment_path: '/Users/someone/crop.png',
    })
  })

  test('omits the attachment fields entirely when there is no attachment', async () => {
    const { calls, fetchImpl } = makeFetch(call => {
      if (call.url.endsWith('/info')) return new Response('{}', { status: 200 })
      return new Response('{"status":"sent"}', { status: 200 })
    })

    await sendRemoteWithKeepalive('http://bridge.local:8432', 'chat-id', 'hello', {
      fetchImpl,
      retryDelayMs: 0,
    })

    const body = JSON.parse(calls[1].body ?? '{}')
    expect(Object.keys(body).sort()).toEqual(['chat_id', 'text'])
  })

  test('carries the attachment through a retry', async () => {
    const { calls, fetchImpl } = makeFetch(call => {
      if (call.url.endsWith('/send') && calls.length === 2) {
        return new Error('connection reset by peer')
      }
      if (call.url.endsWith('/info')) return new Response('{}', { status: 200 })
      return new Response('{"status":"sent","attachment_sent":true}', { status: 200 })
    })

    await sendRemoteWithKeepalive('http://bridge.local:8432', 'chat-id', 'the mark', {
      attachment: { base64: 'aW1hZ2UtYnl0ZXM=', name: 'crop.png' },
      fetchImpl,
      retryDelayMs: 0,
    })

    expect(JSON.parse(calls[3].body ?? '{}').attachment_name).toBe('crop.png')
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
