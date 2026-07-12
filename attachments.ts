/**
 * iMessage image-attachment materialization (mc-iee8).
 *
 * Pure, importable helpers (no MCP/global state) so the channel server and the
 * test suite share one implementation. Given an image attachment, get its bytes
 * (from the remote bridge, or from a local file) onto disk in a
 * harness-readable format (HEIC is converted to PNG via `sips`) and return the
 * path the session can Read.
 */

import * as fs from 'fs'
import * as os from 'os'
import * as path from 'path'

export const DEFAULT_IMAGE_CACHE_DIR = path.join(
  os.homedir(),
  '.claude',
  'image-cache',
  'imessage',
)

const HEIC_MIMES = new Set(['image/heic', 'image/heif'])
const IMAGE_UTIS = new Set(['public.heic', 'public.heif', 'public.jpeg', 'public.png'])

export type AttachmentMeta = {
  index: number
  mime_type: string | null
  transfer_name: string | null
  is_image: boolean
  /** Set in local mode only — the file already lives on this host. */
  localPath?: string
}

export function isImageAttachment(
  mime: string | null,
  uti: string | null,
): boolean {
  if (mime && mime.startsWith('image/')) return true
  if (uti && IMAGE_UTIS.has(uti)) return true
  return false
}

export function pickImageAttachment(
  attachments: AttachmentMeta[],
): AttachmentMeta | undefined {
  return attachments.find(a => a.is_image)
}

/** Sanitize a transfer name down to a safe basename for the cache. */
export function safeName(
  name: string | null | undefined,
  fallback: string,
): string {
  const base = (name ?? '').split('/').pop() ?? ''
  const cleaned = base.replace(/[^A-Za-z0-9._-]/g, '_')
  return cleaned || fallback
}

export async function convertHeicToPng(
  src: string,
  dest: string,
): Promise<boolean> {
  try {
    const proc = Bun.spawn(['sips', '-s', 'format', 'png', src, '--out', dest], {
      stdout: 'ignore',
      stderr: 'pipe',
    })
    return (await proc.exited) === 0
  } catch {
    return false
  }
}

/** Fetch raw attachment bytes from the bridge, or null on any failure. */
export async function fetchAttachmentBytes(
  bridgeUrl: string,
  msgGuid: string,
  index: number,
  timeoutMs = 15000,
): Promise<Uint8Array | null> {
  const url =
    `${bridgeUrl}/attachment?msg=${encodeURIComponent(msgGuid)}&index=${index}`
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(timeoutMs) })
    if (!res.ok) return null
    return new Uint8Array(await res.arrayBuffer())
  } catch {
    return null
  }
}

export type ImageSource =
  | { kind: 'remote'; bridgeUrl: string }
  | { kind: 'local' }

export type MaterializeOpts = {
  guid: string
  attachment: AttachmentMeta
  source: ImageSource
  cacheDir?: string
  /** Optional logger; defaults to no-op. */
  log?: (msg: string) => void
}

/**
 * Materialize one image attachment to disk and return its path, or undefined.
 * HEIC/HEIF are converted to PNG; other image types are saved as-is.
 */
export async function materializeImage(
  opts: MaterializeOpts,
): Promise<string | undefined> {
  const { guid, attachment: att } = opts
  const cacheDir = opts.cacheDir ?? DEFAULT_IMAGE_CACHE_DIR
  const log = opts.log ?? (() => {})
  if (!att.is_image) return undefined

  try {
    fs.mkdirSync(cacheDir, { recursive: true })
    const stem = `${safeName(guid, 'msg')}-${att.index}`
    const rawName = safeName(att.transfer_name, `att-${att.index}`)

    let bytes: Uint8Array | null
    if (opts.source.kind === 'remote') {
      bytes = await fetchAttachmentBytes(opts.source.bridgeUrl, guid, att.index)
      if (!bytes) {
        log(`attachment fetch failed for ${guid}#${att.index}`)
        return undefined
      }
    } else {
      if (!att.localPath || !fs.existsSync(att.localPath)) return undefined
      bytes = new Uint8Array(fs.readFileSync(att.localPath))
    }

    const savePath = path.join(cacheDir, `${stem}-${rawName}`)
    fs.writeFileSync(savePath, bytes)

    const isHeic =
      HEIC_MIMES.has((att.mime_type ?? '').toLowerCase()) ||
      /\.heic$|\.heif$/i.test(rawName)
    if (isHeic) {
      const pngPath = path.join(cacheDir, `${stem}.png`)
      if (await convertHeicToPng(savePath, pngPath)) return pngPath
      log(`HEIC->PNG conversion failed for ${guid}; serving raw`)
    }
    return savePath
  } catch (err) {
    log(`materializeImage error for ${guid}: ${err}`)
    return undefined
  }
}
