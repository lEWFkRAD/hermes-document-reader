/**
 * Bearden Document Reader — Hermes desktop plugin.
 *
 * Native page over the firm OCR service (ocr_service.py on your gateway box :8899):
 * live side-by-side view of the page being scanned and what GRM reads,
 * upload, queue, and finished-file downloads. Same backend as the staff
 * web page; this is the in-app skin.
 *
 * Design: DESIGN.md rules — tokens not literals (inline style with --ui-*
 * vars), flat, hairlines, quiet motion.
 */

import {
  host,
  useQuery,
  PALETTE_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
} from '@hermes/plugin-sdk'
import * as React from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'document-reader'
const CANDIDATES = ['http://localhost:8899', 'http://your-ocr-host:8899']
let SERVICE = null

async function service() {
  if (SERVICE) return SERVICE
  for (const base of CANDIDATES) {
    try {
      const r = await fetch(base + '/api/state', { signal: AbortSignal.timeout(1500) })
      if (r.ok) { SERVICE = base; return base }
    } catch {}
  }
  throw new Error('OCR service unreachable')
}

const hair = '1px solid var(--ui-stroke-tertiary)'
const S = {
  root: { display: 'flex', flexDirection: 'column', height: '100%', fontSize: 13 },
  bar: { display: 'flex', alignItems: 'center', gap: 12, padding: '8px 16px', borderBottom: hair, flexWrap: 'wrap' },
  name: { color: 'var(--ui-text-primary)', fontWeight: 600, maxWidth: '28ch', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
  state: { color: 'var(--ui-text-tertiary)', fontSize: 12 },
  meter: { flex: 1, minWidth: 100, height: 2, background: 'var(--ui-stroke-tertiary)', borderRadius: 1, overflow: 'hidden' },
  fill: { height: '100%', background: 'var(--ui-accent)', transition: 'width 0.4s' },
  btn: { background: 'var(--ui-bg-quaternary)', color: 'var(--ui-text-primary)', border: 'none', padding: '4px 12px', borderRadius: 4, fontSize: 12, fontWeight: 500, cursor: 'pointer' },
  strip: { display: 'flex', gap: 4, padding: '6px 16px', overflowX: 'auto', borderBottom: hair },
  chip: sel => ({
    minWidth: 26, textAlign: 'center', padding: '2px 7px', borderRadius: 4, fontSize: 11.5,
    cursor: 'pointer', userSelect: 'none',
    background: sel ? 'var(--ui-bg-quaternary)' : 'transparent',
  }),
  grid: { flex: 1, display: 'grid', gridTemplateColumns: '1fr 1fr', overflow: 'hidden' },
  pane: { overflow: 'auto', position: 'relative' },
  paneR: { overflow: 'auto', position: 'relative', borderLeft: hair },
  label: {
    position: 'sticky', top: 0, zIndex: 2, background: 'var(--ui-bg-editor, var(--ui-bg-primary))',
    padding: '5px 14px', fontSize: 10.5, letterSpacing: '0.08em', textTransform: 'uppercase',
    color: 'var(--ui-text-quaternary)', borderBottom: hair,
  },
  out: { padding: '14px 18px', lineHeight: 1.55, color: 'var(--ui-text-primary)' },
  pre: { margin: 0, padding: '14px 18px', whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontFamily: 'var(--font-mono, Consolas, monospace)', fontSize: 12, lineHeight: 1.5, color: 'var(--ui-text-secondary)' },
  quiet: { color: 'var(--ui-text-quaternary)', padding: 28, textAlign: 'center' },
  empty: { flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 10 },
  hist: { padding: '8px 16px', borderTop: hair, fontSize: 12 },
  link: { color: 'var(--ui-accent)', marginRight: 12, textDecoration: 'none', fontWeight: 600, cursor: 'pointer' },
}

const BEAM_CSS = `
@keyframes docreader-sweep { 0% { top: -80px; } 55% { top: calc(100% - 20px); } 100% { top: -80px; } }
.docreader-beam { position: absolute; left: 0; right: 0; height: 80px; top: 0; pointer-events: none; z-index: 3;
  background: linear-gradient(180deg, rgba(255,190,90,0) 0%, rgba(255,190,90,0.10) 35%, rgba(255,190,90,0.35) 50%, rgba(255,190,90,0.10) 65%, rgba(255,190,90,0) 100%);
  mix-blend-mode: screen; animation: docreader-sweep 2.2s linear infinite; }
.docreader-beam::after { content: ""; position: absolute; left: 0; right: 0; top: 50%; height: 1.5px;
  background: rgba(255,244,214,0.9); box-shadow: 0 0 10px 2px rgba(255,205,110,0.55); }
@keyframes docreader-blink { to { opacity: 0; } }
.docreader-caret { display: inline-block; width: 6px; height: 1em; vertical-align: text-bottom;
  background: var(--ui-accent); animation: docreader-blink 0.9s steps(2) infinite; }
`

function useServiceState() {
  return useQuery({
    queryKey: ['document-reader', 'state'],
    queryFn: async () => {
      const base = await service()
      const r = await fetch(base + '/api/state')
      if (!r.ok) throw new Error('state fetch failed')
      const data = await r.json()
      return { base, data }
    },
    refetchInterval: 700,
    retry: 1,
  })
}

function PageHtml({ url }) {
  const q = useQuery({
    queryKey: ['document-reader', 'page', url],
    queryFn: async () => (await fetch(url)).text(),
    staleTime: Infinity,
  })
  if (!q.data) return jsx('div', { style: S.quiet, children: 'loading…' })
  return jsx('div', { style: S.out, dangerouslySetInnerHTML: { __html: q.data } })
}

function Reader() {
  const q = useServiceState()
  const [selected, setSelected] = React.useState(null)
  const [showHist, setShowHist] = React.useState(false)
  const [dragging, setDragging] = React.useState(false)
  const fileRef = React.useRef(null)
  const preRef = React.useRef(null)

  const base = q.data?.base
  const st = q.data?.data
  const job = st?.job

  React.useEffect(() => {
    if (preRef.current) preRef.current.scrollTop = preRef.current.scrollHeight
  }, [job?.partial])
  React.useEffect(() => { setSelected(null) }, [job?.id])

  // Prefetch every page image as soon as the job has rendered them, so
  // switching pages (and auto-follow) doesn't pop in late.
  React.useEffect(() => {
    if (!job || !base || !job.total) return
    for (let n = 1; n <= job.total; n++) {
      const img = new Image()
      img.src = `${base}${job.base}/page_${n}.jpg`
    }
  }, [job?.id, job?.total, base])

  if (q.isError) {
    return jsxs('div', { style: { ...S.root, ...S.empty }, children: [
      jsx('div', { style: { color: 'var(--ui-text-secondary)', fontWeight: 600 }, children: 'Document Reader service unreachable' }),
      jsx('div', { style: { color: 'var(--ui-text-quaternary)', fontSize: 12 }, children: 'Expected at your-ocr-host:8899 — is the service running?' }),
    ] })
  }
  if (!st) return jsx('div', { style: { ...S.root, ...S.empty }, children: jsx('div', { style: S.quiet, children: '…' }) })

  const upload = async files => {
    for (const f of files) {
      await fetch(`${base}/api/upload?name=${encodeURIComponent(f.name)}`, { method: 'POST', body: f })
    }
    host.notify({ kind: 'info', message: `${files.length} file(s) sent to the Document Reader` })
  }

  const shown = selected ?? (job ? (job.state === 'finished' ? job.total : job.current || 1) : 0)
  const page = job?.pages?.[shown - 1]

  const toolbar = jsxs('div', { style: S.bar, children: [
    jsx('span', { style: S.name, children: job ? (job.current_file || job.name) : 'Document Reader' }),
    jsx('span', { style: { ...S.state, color: job?.state === 'finished' ? 'var(--ui-accent)' : 'var(--ui-text-tertiary)' },
      children: job ? ({ loading: 'opening file', rendering: 'preparing pages', ocr: `reading page ${Math.min(job.current, job.total)} of ${job.total}`, finished: 'done', failed: 'problem' }[job.state] || job.state)
                    : (st.queue?.length ? 'starting…' : 'ready') }),
    jsx('div', { style: S.meter, children: jsx('div', { style: { ...S.fill, width: job?.total ? `${(100 * job.done) / job.total}%` : '0%' } }) }),
    jsx('button', { style: { ...S.btn, background: 'var(--ui-accent)', color: 'var(--theme-primary-foreground, #fff)' }, type: 'button',
      onClick: () => fileRef.current?.click(), children: 'Scan a document' }),
    jsx('button', { style: S.btn, type: 'button', onClick: () => setShowHist(v => !v), children: 'Finished files' }),
    jsx('input', { ref: fileRef, type: 'file', multiple: true, style: { display: 'none' },
      accept: '.pdf,.png,.jpg,.jpeg,.tiff,.bmp',
      onChange: e => { upload([...e.target.files]); e.target.value = '' } }),
  ] })

  const strip = job ? jsx('div', { style: S.strip, children: (job.pages || []).map(p =>
    jsx('div', {
      style: {
        ...S.chip(p.n === shown),
        color: p.state === 'working' ? 'var(--ui-accent)'
             : p.state === 'error' ? 'var(--ui-text-secondary)'
             : p.state === 'done' ? 'var(--ui-text-secondary)' : 'var(--ui-text-quaternary)',
        fontWeight: p.n === shown || p.state === 'working' ? 600 : 400,
      },
      onClick: () => setSelected(p.n),
      children: String(p.n),
    }, p.n)
  ) }) : null

  const queueLine = st.queue?.length
    ? jsx('div', { style: { padding: '4px 16px', fontSize: 12, color: 'var(--ui-text-quaternary)', borderBottom: hair },
        children: 'Waiting in line: ' + st.queue.map(x => x.name).join('  ·  ') })
    : null

  let right
  if (!job) {
    right = jsx('div', { style: S.quiet, children: 'nothing being read right now' })
  } else if (page?.state === 'done') {
    right = jsx(PageHtml, { url: `${base}${job.base}/page_${shown}.html` })
  } else if (page?.state === 'working') {
    const text = job.partial || ''
    right = jsxs('pre', { style: S.pre, children: [text, jsx('span', { className: 'docreader-caret' })] })
  } else if (page?.state === 'error') {
    right = jsx('div', { style: S.quiet, children: `this page could not be read: ${page.error || ''}` })
  } else {
    right = jsx('div', { style: S.quiet, children: 'not read yet' })
  }

  const left = job
    ? jsxs('div', { style: { position: 'relative' }, children: [
        jsx('img', { src: `${base}${job.base}/page_${shown}.jpg`, style: { width: '100%', display: 'block' }, alt: `page ${shown}` }),
        page?.state === 'working' ? jsx('div', { className: 'docreader-beam' }) : null,
      ] })
    : jsxs('div', { style: { ...S.empty, height: '100%' }, children: [
        jsx('div', { style: { color: 'var(--ui-text-secondary)', fontWeight: 600 }, children: 'Drop-free zone — use "Scan a document"' }),
        jsx('div', { style: { color: 'var(--ui-text-quaternary)', fontSize: 12, textAlign: 'center', lineHeight: 1.7 },
          children: 'or save a scanned PDF into \\\\YOUR-SERVER\\M\\OCR-Inbox. Finished Excel and text files land in OCR-Inbox\\Processed.' }),
      ] })

  const copyPath = (p, what) => {
    if (!p) return
    navigator.clipboard.writeText(p).then(
      () => host.notify({ kind: 'info', message: `${what} path copied: ${p}` }),
      () => host.notify({ kind: 'error', message: 'could not copy path' }),
    )
  }

  const pathChip = (label, p) => p ? jsx('button', {
    type: 'button',
    style: { ...S.btn, padding: '2px 8px', fontSize: 11 },
    onClick: () => copyPath(p, label),
    onContextMenu: ev => { ev.preventDefault(); copyPath(p, label) },
    children: `Copy ${label} path`,
  }) : null

  const hist = showHist ? jsxs('div', { style: { maxHeight: 220, overflowY: 'auto', borderTop: hair },
    children: [
      jsxs('div', { style: { ...S.hist, display: 'flex', alignItems: 'center', gap: 10 }, children: [
        jsx('span', { style: { color: 'var(--ui-text-tertiary)', fontSize: 12, fontFamily: 'var(--font-mono, Consolas, monospace)' },
          children: st.history?.[0]?.paths?.folder || 'D:\\OCR-Inbox\\Processed' }),
        pathChip('folder', st.history?.[0]?.paths?.folder || 'D:\\OCR-Inbox\\Processed'),
      ] }),
      ...((st.history || []).length ? st.history.map(e =>
        jsxs('div', {
          style: S.hist,
          onContextMenu: ev => { ev.preventDefault(); copyPath(e.paths?.xlsx || e.paths?.txt || e.paths?.folder, e.name) },
          children: [
            jsx('div', { style: { color: 'var(--ui-text-primary)', fontWeight: 500 }, children: e.name }),
            jsx('div', { style: { color: 'var(--ui-text-quaternary)', margin: '2px 0 4px' },
              children: `${e.when} · ${e.pages} pages · ${Math.round(e.secs)}s${e.errors ? ` · ${e.errors} page(s) failed` : ''}` }),
            jsxs('div', { style: { display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }, children: [
              e.links?.xlsx ? jsx('a', { style: S.link, href: base + e.links.xlsx, children: 'Excel' }) : null,
              jsx('a', { style: S.link, href: base + e.links.md, children: 'Text' }),
              pathChip('Excel', e.paths?.xlsx),
              pathChip('text', e.paths?.txt),
            ] }),
          ],
        }, e.id)
      ) : [jsx('div', { style: { ...S.hist, color: 'var(--ui-text-quaternary)' }, children: 'nothing yet' }, 'empty')]),
    ] }) : null

  return jsxs('div', {
    style: { ...S.root, outline: dragging ? '1.5px dashed var(--ui-accent)' : 'none', outlineOffset: -6 },
    onDragOver: e => { e.preventDefault(); e.stopPropagation(); setDragging(true) },
    onDragLeave: e => { e.preventDefault(); setDragging(false) },
    onDrop: e => {
      e.preventDefault(); e.stopPropagation(); setDragging(false)
      const files = [...(e.dataTransfer?.files || [])]
      if (files.length) upload(files)
    },
    children: [
    jsx('style', { children: BEAM_CSS }),
    toolbar,
    queueLine,
    strip,
    jsxs('div', { style: S.grid, children: [
      jsxs('div', { style: S.pane, children: [
        jsx('div', { style: S.label, children: `Scanned page${job ? ` — p.${shown}` : ''}` }),
        left,
      ] }),
      jsxs('div', { style: S.paneR, children: [
        jsx('div', { style: S.label,
          children: `What the computer reads${page?.state === 'done' ? ` — p.${shown} · ${page.secs}s · ${page.chars} chars` : page?.state === 'working' ? ` — p.${shown} · reading…` : ''}` }),
        right,
      ] }),
    ] }),
    hist,
  ] })
}

export default {
  id: ID,
  name: 'Document Reader',
  defaultEnabled: false, // inventories in Settings → Plugins; enable in Settings → Plugins
  register(ctx) {
    ctx.i18n.register({
      en: {
        navLabel: 'Document Reader',
        paletteOpen: 'Document Reader: Open',
      },
    })

    ctx.register({
      id: 'page',
      area: ROUTES_AREA,
      data: { path: '/document-reader' },
      render: () => jsx(Reader, {}),
    })

    ctx.register({
      id: 'nav',
      area: SIDEBAR_NAV_AREA,
      data: { path: '/document-reader', label: ctx.i18n.t('navLabel'), codicon: 'file-pdf' },
    })

    ctx.register({
      id: 'open',
      area: PALETTE_AREA,
      data: {
        id: 'document-reader.open',
        label: ctx.i18n.t('paletteOpen'),
        keywords: ['ocr', 'scan', 'pdf', 'document', 'reader'],
        run: () => host.navigate('/document-reader'),
      },
    })
  },
}
