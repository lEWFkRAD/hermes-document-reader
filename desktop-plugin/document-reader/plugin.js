/** Profile-aware Hermes Desktop and HUD extension for Document Reader. */

import {
  COMPOSER_AREAS,
  PALETTE_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  host,
  queryClient,
  useQuery,
  useValue,
} from '@hermes/plugin-sdk'
import * as React from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'document-reader'
const VERSION = '0.1.0'
const MAX_FILES = 10
const MAX_FILE_BYTES = 100 * 1024 * 1024
const ACCEPT = '.pdf,.png,.jpg,.jpeg,.tiff,.bmp'
const TYPES = new Set(['pdf', 'png', 'jpg', 'jpeg', 'tiff', 'bmp'])
const MIME = {
  pdf: 'application/pdf', png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg',
  tiff: 'image/tiff', bmp: 'image/bmp',
}
const hairline = '1px solid var(--ui-stroke-tertiary)'

const S = {
  root: { display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0, fontSize: 13 },
  toolbar: { display: 'flex', alignItems: 'center', gap: 10, padding: '8px 14px', borderBottom: hairline, flexWrap: 'wrap' },
  title: { color: 'var(--ui-text-primary)', fontWeight: 650, maxWidth: '30ch', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
  quiet: { color: 'var(--ui-text-quaternary)' },
  button: { border: 0, borderRadius: 4, background: 'var(--ui-bg-quaternary)', color: 'var(--ui-text-primary)', padding: '5px 10px', cursor: 'pointer', fontSize: 12 },
  primary: { background: 'var(--ui-accent)', color: 'var(--theme-primary-foreground, #fff)' },
  meter: { flex: 1, minWidth: 90, height: 2, overflow: 'hidden', background: 'var(--ui-stroke-tertiary)' },
  queue: { display: 'flex', gap: 6, overflowX: 'auto', padding: '6px 14px', borderBottom: hairline, color: 'var(--ui-text-tertiary)' },
  pages: { display: 'flex', gap: 4, overflowX: 'auto', padding: '6px 14px', borderBottom: hairline },
  page: selected => ({ minWidth: 28, padding: '3px 7px', border: 0, borderRadius: 4, cursor: 'pointer', color: selected ? 'var(--ui-text-primary)' : 'var(--ui-text-tertiary)', background: selected ? 'var(--ui-bg-quaternary)' : 'transparent' }),
  grid: { display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1fr)', minHeight: 0, flex: 1 },
  pane: { position: 'relative', overflow: 'auto', minWidth: 0 },
  right: { position: 'relative', overflow: 'auto', minWidth: 0, borderLeft: hairline },
  label: { position: 'sticky', top: 0, zIndex: 5, padding: '5px 12px', borderBottom: hairline, background: 'var(--ui-bg-primary)', color: 'var(--ui-text-quaternary)', fontSize: 10, letterSpacing: '.08em', textTransform: 'uppercase' },
  empty: { height: '100%', minHeight: 180, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 10, padding: 24, textAlign: 'center' },
  pre: { margin: 0, padding: 16, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', color: 'var(--ui-text-secondary)', fontFamily: 'var(--font-mono, Consolas, monospace)', lineHeight: 1.5 },
  history: { maxHeight: 230, overflowY: 'auto', borderTop: hairline },
  row: { padding: '8px 14px', borderBottom: hairline },
  overlay: { position: 'absolute', inset: 8, zIndex: 20, display: 'flex', alignItems: 'center', justifyContent: 'center', border: '1.5px dashed var(--ui-accent)', borderRadius: 8, background: 'color-mix(in srgb, var(--ui-bg-primary) 88%, transparent)', color: 'var(--ui-text-primary)', fontWeight: 650, pointerEvents: 'none' },
}
const OFFSCREEN_INPUT = { position: 'fixed', left: -10000, top: -10000, width: 1, height: 1, opacity: 0, pointerEvents: 'none' }

function extension(name) {
  const match = String(name || '').toLowerCase().match(/\.([a-z0-9]+)$/)
  return match ? match[1] : ''
}

function validateFiles(input) {
  const files = [...(input || [])]
  if (!files.length) return []
  if (files.length > MAX_FILES) throw new Error(`Choose at most ${MAX_FILES} files at once`)
  for (const file of files) {
    const ext = extension(file.name)
    if (!TYPES.has(ext)) throw new Error(`${file.name}: unsupported file type`)
    if (!Number.isFinite(file.size) || file.size <= 0) throw new Error(`${file.name}: file is empty`)
    if (file.size > MAX_FILE_BYTES) throw new Error(`${file.name}: exceeds the 100 MiB limit`)
  }
  return files
}

function isFileDrag(event) {
  return [...(event.dataTransfer?.types || [])].includes('Files')
}

function openFileInput(input) {
  if (!input) return
  try {
    if (typeof input.showPicker === 'function') input.showPicker()
    else input.click()
  } catch {
    input.click()
  }
}

function validIdentity(value) {
  return value && /^[a-z0-9][a-z0-9_-]{0,63}$/.test(String(value.profile || ''))
    && /^[0-9a-f]{64}$/.test(String(value.profile_fingerprint || ''))
}

function humanJobState(value) {
  return ({
    loading: 'opening file',
    rendering: 'preparing pages',
    ocr: 'recognizing text',
    finished: 'complete',
    finished_with_errors: 'complete with review needed',
    failed: 'failed',
    cancelled: 'stopped',
    quarantined: 'quarantined',
  })[String(value || '')] || 'ready'
}

async function uploadFiles(ctx, input, expected = null) {
  const files = validateFiles(input)
  if (!files.length) return 0
  let identity = expected
  if (!validIdentity(identity)) identity = await ctx.rest('/state', { timeoutMs: 5000 })
  if (!validIdentity(identity)) throw new Error('profile identity is unavailable')
  if (expected?.profile && String(identity.profile) !== String(expected.profile)) {
    throw new Error('profile changed before upload')
  }
  const assertion = `expected_profile=${encodeURIComponent(identity.profile)}&expected_fingerprint=${encodeURIComponent(identity.profile_fingerprint)}`
  let accepted = 0
  for (const file of files) {
    const ext = extension(file.name)
    const bytes = await file.arrayBuffer()
    if (bytes.byteLength !== file.size || bytes.byteLength > MAX_FILE_BYTES) {
      throw new Error(`${file.name}: file changed while it was being read`)
    }
    const result = await ctx.rest(`/upload?${assertion}`, {
      method: 'POST',
      upload: { filename: file.name, contentType: MIME[ext], bytes },
      timeoutMs: 180000,
    })
    if (!result?.ok) throw new Error(`${file.name}: service did not accept the upload`)
    if (result.profile !== identity.profile || result.profile_fingerprint !== identity.profile_fingerprint) {
      throw new Error('profile changed during upload')
    }
    accepted += 1
  }
  return accepted
}

function chooseFiles() {
  return new Promise((resolve, reject) => {
    const input = document.createElement('input')
    input.type = 'file'
    input.multiple = true
    input.accept = ACCEPT
    input.style.cssText = 'position:fixed;left:-10000px;top:-10000px;width:1px;height:1px;opacity:0;pointer-events:none'
    let settled = false
    let focusTimer = null
    const cleanup = () => {
      if (focusTimer !== null) window.clearTimeout(focusTimer)
      window.removeEventListener('focus', onFocus)
      input.removeEventListener('change', onChange)
      input.removeEventListener('cancel', onCancel)
      input.remove()
    }
    const finish = files => {
      if (settled) return
      settled = true
      cleanup()
      resolve(files)
    }
    const fail = error => {
      if (settled) return
      settled = true
      cleanup()
      reject(error)
    }
    const onChange = () => finish([...(input.files || [])])
    const onCancel = () => finish([])
    const onFocus = () => {
      focusTimer = window.setTimeout(() => {
        if (!input.files?.length) finish([])
      }, 400)
    }
    input.addEventListener('change', onChange)
    input.addEventListener('cancel', onCancel)
    window.addEventListener('focus', onFocus)
    document.body.appendChild(input)
    try {
      if (typeof input.showPicker === 'function') input.showPicker()
      else input.click()
    } catch (firstError) {
      try { input.click() } catch { fail(firstError) }
    }
  })
}

async function hudUpload(ctx, insertText) {
  const files = await chooseFiles()
  if (!files.length) return
  try {
    const count = await uploadFiles(ctx, files)
    insertText(`\n[Document Reader queued ${count} file${count === 1 ? '' : 's'}]\n`)
    host.notify({ kind: 'info', message: `${count} document${count === 1 ? '' : 's'} queued` })
  } catch {
    host.notify({ kind: 'error', message: 'Document Reader could not finish the batch; check its queue' })
  }
}

function useProfileReset() {
  const profile = useValue(host.state.profile) || 'default'
  const previous = React.useRef(profile)
  React.useEffect(() => {
    if (previous.current !== profile) {
      queryClient.removeQueries({ queryKey: [ID, previous.current] })
      previous.current = profile
    }
  }, [profile])
  return profile
}

function useReaderState(ctx, profile) {
  return useQuery({
    queryKey: [ID, profile, 'state'],
    queryFn: () => ctx.rest('/state', { timeoutMs: 5000 }),
    refetchInterval: 850,
    retry: 1,
  })
}

function useAsset(ctx, profile, jobId, filename) {
  return useQuery({
    queryKey: [ID, profile, 'asset', jobId, filename],
    queryFn: () => ctx.rest(`/asset/${encodeURIComponent(jobId)}/${encodeURIComponent(filename)}`, { timeoutMs: 30000 }),
    enabled: Boolean(jobId && filename),
    staleTime: Infinity,
    gcTime: 15000,
    retry: 1,
  })
}

function decodeBase64(value) {
  const raw = atob(value)
  const bytes = new Uint8Array(raw.length)
  for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i)
  return bytes
}

function PageImage({ ctx, profile, jobId, page }) {
  const asset = useAsset(ctx, profile, jobId, `page_${page}.jpg`)
  if (asset.isError) return jsx('div', { style: S.empty, children: 'Page preview unavailable' })
  if (!asset.data?.data) return jsx('div', { style: S.empty, children: 'Loading page preview…' })
  const src = `data:${asset.data.content_type};base64,${asset.data.data}`
  return jsx('img', { src, alt: `Page ${page}`, style: { width: '100%', display: 'block' } })
}

function PageText({ ctx, profile, job, page }) {
  const pageState = job?.pages?.[page - 1]
  const asset = useAsset(ctx, profile, job?.id, pageState?.state === 'done' ? `page_${page}.html` : '')
  if (pageState?.state === 'working') return jsx('pre', { style: S.pre, children: job.partial || 'Reading…' })
  if (pageState?.state === 'error') return jsx('div', { style: S.empty, children: `Page could not be read: ${pageState.error || 'unknown error'}` })
  if (pageState?.state !== 'done') return jsx('div', { style: S.empty, children: 'Not read yet' })
  if (asset.isError) return jsx('div', { style: S.empty, children: 'Recognized text unavailable' })
  if (!asset.data?.html) return jsx('div', { style: S.empty, children: 'Loading recognized text…' })
  const parsed = new DOMParser().parseFromString(asset.data.html, 'text/html')
  return jsx('pre', { style: S.pre, children: parsed.body.textContent || '' })
}

async function downloadAsset(ctx, jobId, filename) {
  try {
    const asset = await ctx.rest(`/asset/${encodeURIComponent(jobId)}/${encodeURIComponent(filename)}`, { timeoutMs: 60000 })
    if (asset?.kind !== 'binary' || asset.encoding !== 'base64') throw new Error('download response was invalid')
    const blob = new Blob([decodeBase64(asset.data)], { type: asset.content_type })
    const url = URL.createObjectURL(blob)
    try {
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = asset.name || filename
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
    } finally {
      window.setTimeout(() => URL.revokeObjectURL(url), 0)
    }
  } catch (error) {
    host.notify({ kind: 'error', message: `Download failed: ${error?.message || error}` })
  }
}

function Reader({ ctx }) {
  const profile = useProfileReset()
  const query = useReaderState(ctx, profile)
  const [selected, setSelected] = React.useState(null)
  const [historyOpen, setHistoryOpen] = React.useState(false)
  const [dragging, setDragging] = React.useState(false)
  const [uploading, setUploading] = React.useState(false)
  const inputRef = React.useRef(null)
  const dragDepth = React.useRef(0)
  const inFlight = React.useRef(false)
  const previousJob = React.useRef(null)
  const state = query.data?.service
  const job = state?.job

  React.useEffect(() => {
    setSelected(null)
    setHistoryOpen(false)
    setDragging(false)
    dragDepth.current = 0
  }, [profile])
  React.useEffect(() => setSelected(null), [job?.id])
  React.useEffect(() => {
    if (previousJob.current && previousJob.current !== job?.id) {
      queryClient.removeQueries({ queryKey: [ID, profile, 'asset', previousJob.current] })
    }
    previousJob.current = job?.id || null
  }, [profile, job?.id])

  const send = React.useCallback(async input => {
    if (inFlight.current) return
    inFlight.current = true
    setUploading(true)
    try {
      const count = await uploadFiles(ctx, input, {
        profile,
        profile_fingerprint: query.data?.profile_fingerprint,
      })
      if (count) host.notify({ kind: 'info', message: `${count} document${count === 1 ? '' : 's'} queued for ${profile}` })
      await query.refetch()
    } catch (error) {
      host.notify({ kind: 'error', message: `Document Reader upload failed: ${error?.message || error}` })
    } finally {
      inFlight.current = false
      setUploading(false)
    }
  }, [ctx, profile, query])

  const onDragEnter = event => {
    if (!isFileDrag(event)) return
    event.preventDefault(); event.stopPropagation(); dragDepth.current += 1; setDragging(true)
  }
  const onDragOver = event => {
    if (!isFileDrag(event)) return
    event.preventDefault(); event.stopPropagation(); event.dataTransfer.dropEffect = 'copy'
  }
  const onDragLeave = event => {
    if (!isFileDrag(event) && dragDepth.current === 0) return
    event.preventDefault(); event.stopPropagation(); dragDepth.current = Math.max(0, dragDepth.current - 1)
    if (dragDepth.current === 0) setDragging(false)
  }
  const onDrop = event => {
    if (!isFileDrag(event)) return
    event.preventDefault(); event.stopPropagation(); dragDepth.current = 0; setDragging(false)
    const files = [...(event.dataTransfer?.files || [])]
    if (files.length) void send(files)
  }

  if (query.isError) {
    const selector = profile === 'default' ? '' : `-p ${profile} `
    return jsxs('div', { style: { ...S.root, ...S.empty }, children: [
      jsx('strong', { children: `Document Reader is not ready for profile “${profile}”` }),
      jsx('span', { style: S.quiet, children: `Run hermes ${selector}document-reader status, then configure, install, or recover as reported.` }),
    ] })
  }
  if (!state) return jsx('div', { style: { ...S.root, ...S.empty }, children: 'Loading Document Reader…' })

  const shown = selected ?? (job ? Math.max(1, job.state === 'finished' ? job.total : job.current || 1) : 0)
  const progress = job?.total ? `${Math.min(100, (100 * job.done) / job.total)}%` : '0%'
  const queue = state.queue || []
  const active = Boolean(job && !['finished', 'finished_with_errors', 'failed', 'cancelled', 'quarantined'].includes(job.state))

  const cancel = async () => {
    try {
      await ctx.rest('/cancel', { method: 'POST', timeoutMs: 5000 })
      host.notify({ kind: 'info', message: 'Stopping the current document' })
    } catch (error) {
      host.notify({ kind: 'error', message: `Could not stop the document: ${error?.message || error}` })
    }
  }

  const history = historyOpen ? jsx('div', { style: S.history, children: (state.history || []).length
    ? state.history.map(item => jsxs('div', { style: S.row, children: [
        jsx('div', { style: S.title, children: item.name }),
        jsx('div', { style: { ...S.quiet, margin: '3px 0 6px' }, children: `${item.when} · ${item.pages} page${item.pages === 1 ? '' : 's'} · ${Math.round(item.secs)}s` }),
        jsxs('div', { style: { display: 'flex', gap: 6, flexWrap: 'wrap' }, children: [
          ...Object.entries(item.files || {}).map(([kind, name]) => jsx('button', { type: 'button', style: S.button, onClick: () => void downloadAsset(ctx, item.id, name), children: `Download ${kind.toUpperCase()}` }, `${kind}:${name}`)),
        ] }),
      ] }, item.id))
    : jsx('div', { style: S.row, children: 'No completed documents yet' }) }) : null

  return jsxs('div', {
    style: S.root,
    onDragEnterCapture: onDragEnter,
    onDragOverCapture: onDragOver,
    onDragLeaveCapture: onDragLeave,
    onDropCapture: onDrop,
    children: [
      dragging ? jsx('div', { style: S.overlay, children: 'Drop up to 10 supported documents' }) : null,
      jsxs('div', { style: S.toolbar, children: [
        jsx('span', { style: S.title, children: job?.current_file || 'Document Reader' }),
        jsx('span', { style: S.quiet, children: `${profile} · ${humanJobState(job?.state)}` }),
        jsx('div', { style: S.meter, children: jsx('div', { style: { width: progress, height: '100%', background: 'var(--ui-accent)', transition: 'width .3s' } }) }),
        jsx('button', { type: 'button', style: { ...S.button, ...S.primary }, disabled: uploading, onClick: () => openFileInput(inputRef.current), children: uploading ? 'Uploading…' : 'Scan documents' }),
        job && !['finished', 'finished_with_errors', 'failed', 'cancelled', 'quarantined'].includes(job.state)
          ? jsx('button', { type: 'button', style: S.button, onClick: () => void cancel(), children: 'Stop' }) : null,
        jsx('button', { type: 'button', style: S.button, onClick: () => setHistoryOpen(value => !value), children: historyOpen ? 'Hide finished' : 'Finished files' }),
        jsx('input', { ref: inputRef, type: 'file', accept: ACCEPT, multiple: true, style: OFFSCREEN_INPUT, tabIndex: -1, onChange: event => {
          const files = [...(event.target.files || [])]; event.target.value = ''; if (files.length) void send(files)
        } }),
      ] }),
      jsx('div', { style: S.queue, children: [
        jsx('strong', { children: `Queue · ${active ? '1 active' : 'clear'} · ${queue.length} waiting` }),
        ...queue.slice(0, 12).map((item, index) => jsx('span', { children: item.name }, `${index}:${item.name}`)),
        queue.length > 12 ? jsx('span', { children: `+${queue.length - 12} more` }) : null,
      ] }),
      job ? jsx('div', { style: S.pages, children: (job.pages || []).map(page => jsx('button', { type: 'button', style: S.page(page.n === shown), onClick: () => setSelected(page.n), children: String(page.n) }, page.n)) }) : null,
      jsxs('div', { style: S.grid, children: [
        jsxs('div', { style: S.pane, children: [
          jsx('div', { style: S.label, children: `Scanned page${shown ? ` · ${shown}` : ''}` }),
          job && shown ? jsx(PageImage, { ctx, profile, jobId: job.id, page: shown }) : jsxs('div', {
            style: { ...S.empty, cursor: 'pointer' },
            role: 'button',
            tabIndex: 0,
            onClick: () => openFileInput(inputRef.current),
            onKeyDown: event => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault(); openFileInput(inputRef.current)
              }
            },
            children: [jsx('strong', { children: 'Drop documents here' }), jsx('span', { style: S.quiet, children: 'Click or press Enter · PDF, PNG, JPEG, TIFF, or BMP · 100 MiB each · 10 at a time' })],
          }),
        ] }),
        jsxs('div', { style: S.right, children: [
          jsx('div', { style: S.label, children: `Recognized text${shown ? ` · ${shown}` : ''}` }),
          job && shown ? jsx(PageText, { ctx, profile, job, page: shown }) : jsx('div', { style: S.empty, children: 'Recognized text appears here' }),
        ] }),
      ] }),
      history,
    ],
  })
}

export default {
  id: ID,
  version: VERSION,
  name: 'Document Reader',
  description: `Profile-scoped Document Reader (${VERSION}) with Desktop and HUD uploads`,
  defaultEnabled: false,
  register(ctx) {
    ctx.register({ id: 'page', area: ROUTES_AREA, data: { path: '/document-reader' }, render: () => jsx(Reader, { ctx }) })
    ctx.register({ id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: '/document-reader', label: 'Document Reader', codicon: 'file-pdf' } })
    ctx.register({ id: 'open', area: PALETTE_AREA, data: { id: 'document-reader.open', label: 'Document Reader: Open', keywords: ['ocr', 'scan', 'pdf', 'document'], run: () => host.navigate('/document-reader') } })
    ctx.register({
      id: 'hud-attachment',
      area: COMPOSER_AREAS.attachments,
      data: { label: 'Scan with Document Reader', icon: 'file-pdf', run: ({ insertText }) => hudUpload(ctx, insertText) },
    })
  },
}
