import { host, PALETTE_AREA, ROUTE_AREA, SIDEBAR_AREA } from '@hermes/plugin-sdk';
import * as React from 'react';
import { jsx } from 'react/jsx-runtime';

function mountReader(container) {
  const BASE = 'http://127.0.0.1:8898/api';
  const TOKEN = '__READER_LOCAL_TOKEN__';

      let disposed = false, generation = 0, allowed = false, token = '', active = null;
      const root = document.createElement('section');
      root.className = 'anydoc-reader';
      root.innerHTML = `<style>
        .anydoc-reader{--dr-bg:var(--background,#10151c);--dr-card:var(--card,#171e28);--dr-text:var(--foreground,#e0e7ef);--dr-muted:var(--muted-foreground,#92a1b4);--dr-border:var(--border,#303b49);--dr-accent:var(--accent,#89b7ed);height:100%;min-height:440px;display:flex;flex-direction:column;background:var(--dr-bg);color:var(--dr-text);font:13px system-ui;box-sizing:border-box}
        .anydoc-reader *{box-sizing:border-box}.anydoc-reader button{border:1px solid var(--dr-border);border-radius:7px;background:var(--dr-card);color:var(--dr-text);padding:8px 13px;cursor:pointer;font:inherit}.anydoc-reader button:hover:not(:disabled){border-color:var(--dr-accent)}.anydoc-reader button:disabled{opacity:.4;cursor:default}.anydoc-reader [data-convert]{background:#28486b;border-color:#45698d;color:#edf5ff}
        .dr-header{display:flex;align-items:center;justify-content:space-between;gap:18px;padding:18px 22px;border-bottom:1px solid var(--dr-border)}.dr-header h2{font-size:18px;font-weight:600;margin:0 0 5px}.dr-subtitle{color:var(--dr-muted);font-size:12px}.dr-actions{display:flex;gap:8px;align-items:center}.dr-status{padding:10px 22px;color:var(--dr-muted);border-bottom:1px solid var(--dr-border);font-size:12px}.dr-status:before{content:'●';color:#80bb9b;margin-right:8px}
        .dr-workspace{flex:1;min-height:0;display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr)}.dr-pane{min-width:0;min-height:0;display:flex;flex-direction:column}.dr-pane:first-child{border-right:1px solid var(--dr-border)}.dr-pane-head{display:flex;align-items:center;justify-content:space-between;padding:13px 18px;border-bottom:1px solid var(--dr-border);background:var(--dr-card);gap:10px}.dr-label{font-size:10px;letter-spacing:1.5px;font-weight:700;color:var(--dr-muted)}.dr-filename{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:70%;font-size:12px}.dr-source{flex:1;min-height:0;overflow:auto;position:relative;background:#0d1118}.dr-source iframe{width:100%;height:100%;min-height:360px;border:0;display:block;background:#c5c5c5}.dr-source pre{margin:24px;padding:26px;background:#f3f1ec;color:#242a32;white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.7;border-radius:3px;min-height:260px;font:13px ui-monospace,monospace}.dr-empty{height:100%;min-height:300px;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;gap:12px;padding:35px;color:var(--dr-muted)}.dr-empty strong{color:var(--dr-text);font-size:16px;font-weight:500}.dr-icon{width:54px;height:65px;border:1px solid var(--dr-border);border-radius:7px;font-size:26px;display:grid;place-items:center;margin-bottom:6px}.dr-output{flex:1;min-height:0;overflow:auto;position:relative}.dr-output pre{margin:0;padding:24px;white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.8 ui-monospace,SFMono-Regular,Consolas,monospace;user-select:text}.dr-output pre:empty{display:none}.dr-output:has(pre:not(:empty)) .dr-empty{display:none}.dr-footer{min-height:39px;padding:11px 22px;border-top:1px solid var(--dr-border);color:var(--dr-muted);font-size:12px}.dr-progress{height:2px;background:transparent;overflow:hidden}.anydoc-reader[data-busy=true] .dr-progress:after{content:'';display:block;height:100%;width:35%;background:#82b5ee;animation:dr-progress 1.6s ease-in-out infinite}@keyframes dr-progress{from{transform:translateX(-100%)}to{transform:translateX(390%)}}@media(prefers-reduced-motion:reduce){.anydoc-reader[data-busy=true] .dr-progress:after{animation:none;width:100%}}@media(max-width:650px){.dr-header{flex-wrap:wrap}.dr-actions{flex-wrap:wrap}.dr-workspace{grid-template-columns:1fr}.dr-pane{min-height:300px}.anydoc-reader{overflow:auto}}
        .dr-source-stage{flex:1;min-height:0;position:relative;display:flex;overflow:hidden}.dr-source-stage .dr-source{width:100%}.dr-scan{display:none;position:absolute;inset:42px 18px 12px;pointer-events:none;z-index:3;overflow:hidden;border-radius:4px}.anydoc-reader[data-busy=true] .dr-scan{display:block}.dr-scan:before{content:'';position:absolute;left:0;right:0;top:-95px;height:95px;background:linear-gradient(transparent,color-mix(in srgb,var(--ui-success,#55e6a0) 16%,transparent));border-bottom:2px solid var(--ui-success,#55e6a0);box-shadow:0 9px 24px -10px var(--ui-success,#55e6a0);animation:dr-page-scan 2.8s linear infinite}.dr-scan-badge{position:absolute;right:12px;top:12px;background:var(--dr-card);border:1px solid var(--ui-success,#55e6a0);color:var(--ui-success,#55e6a0);padding:6px 10px;border-radius:20px;font:10px ui-monospace,monospace;letter-spacing:1.2px;box-shadow:0 2px 12px #0004}.dr-scan-badge:before{content:'●';margin-right:7px;animation:dr-scan-pulse 1s ease-in-out infinite}@keyframes dr-page-scan{from{top:-95px}to{top:100%}}@keyframes dr-scan-pulse{50%{opacity:.3}}@media(prefers-reduced-motion:reduce){.dr-scan:before{animation:none;top:40%}.dr-scan-badge:before{animation:none}}
        .dr-page-controls{display:flex;align-items:center;gap:7px;white-space:nowrap;font-size:11px}.dr-page-controls[hidden]{display:none}.dr-page-controls button{padding:2px 8px}.dr-page-image{display:block;width:calc(100% - 40px);height:auto;margin:20px;box-shadow:0 3px 20px #0005}.dr-dragover .dr-source-stage{outline:2px dashed var(--dr-accent);outline-offset:-7px}.dr-dragover .dr-source{opacity:.55}
      </style>
      <header class="dr-header"><div><h2>Document Reader</h2><div class="dr-subtitle">Original pages alongside extracted text</div></div><div class="dr-actions"><input data-file type="file" hidden accept=".pdf,.docx,.xlsx,.pptx,.csv"><button data-choose>Open document</button><button data-convert disabled>Read document</button><button data-save disabled>Save Markdown</button><button data-health title="Reconnect to local reader" aria-label="Reconnect">↻</button></div></header>
      <div class="dr-status" data-status>Checking reader…</div><div class="dr-progress" role="progressbar" aria-label="Document conversion" hidden></div>
      <main class="dr-workspace"><section class="dr-pane"><div class="dr-pane-head"><span class="dr-label">SOURCE DOCUMENT</span><span class="dr-filename" data-filename>No document selected</span></div><div class="dr-source-stage"><div class="dr-source" data-source><div class="dr-empty"><span class="dr-icon">▤</span><strong>Your pages go here</strong><span>Open a PDF to view its pages while it is read.<br>PDF · Word · Excel · PowerPoint · CSV</span></div></div><div class="dr-scan" aria-hidden="true"><span class="dr-scan-badge">READING</span></div></div></section>
      <section class="dr-pane"><div class="dr-pane-head"><span class="dr-label">EXTRACTED TEXT</span><span class="dr-filename">Markdown</span></div><div class="dr-output"><div class="dr-empty"><span class="dr-icon">≡</span><strong>Output, side by side</strong><span data-output-hint>Extracted text appears here when processing completes.</span></div><pre data-result></pre></div></section></main><footer class="dr-footer" data-detail>AnyDoc · local network processing</footer>`;
      container.append(root);
      const $ = s => root.querySelector(s), status = $('[data-status]'), result = $('[data-result]'), detail = $('[data-detail]');
      const convert = $('[data-convert]'), save = $('[data-save]'), fileInput = $('[data-file]');
      const unsubs = [];
      const source = $('[data-source]'), emptySource = source.innerHTML;
      let sourceURL = null, selection = 0, selectedFile = null, stagedId = null;
      let previewRequest = null, pageRequest = null, currentPage = 1, pageCount = 0, maxBytes = 0;
      const pager = document.createElement('div'); pager.className = 'dr-page-controls'; pager.hidden = true;
      pager.innerHTML = '<button data-prev aria-label="Previous page">‹</button><span data-page>Page 1</span><button data-next aria-label="Next page">›</button>';
      source.closest('.dr-pane').querySelector('.dr-pane-head').append(pager);
      function resetPreview() {
        selection++; previewRequest?.abort(); pageRequest?.abort(); previewRequest = null; pageRequest = null;
        if (sourceURL) URL.revokeObjectURL(sourceURL);
        sourceURL = null; source.innerHTML = emptySource; stagedId = null; selectedFile = null;
        currentPage = 1; pageCount = 0; pager.hidden = true;
        $('[data-filename]').textContent = 'No document selected';
      }
      function busy(value) {
        root.dataset.busy = String(value);
        $('[role=progressbar]').hidden = !value;
        $('[data-choose]').disabled = value;
        fileInput.disabled = value;
        $('[data-output-hint]').textContent = value ? 'Reading the document… output appears when processing completes.' : 'Extracted text appears here when processing completes.';
      }
      function sourceMessage(message) {
        const note = document.createElement('div'); note.className = 'dr-empty'; note.textContent = message; source.replaceChildren(note);
      }
      async function showPage(number) {
        const epoch = generation, selected = selection, id = stagedId;
        if (!id || !pageCount || number < 1 || number > pageCount) return;
        pageRequest?.abort(); const request = new AbortController(); pageRequest = request;
        try {
          const response = await fetch(`${BASE}/jobs/${id}/pages/${number}`, {headers:{Authorization:`Bearer ${token}`},signal:request.signal});
          if (!response.ok) throw new Error(`Page preview: HTTP ${response.status}`);
          const image = await response.blob();
          if (disposed || epoch !== generation || selected !== selection || request.signal.aborted) return;
          const url = URL.createObjectURL(image), img = document.createElement('img');
          img.alt = `Original document, page ${number} of ${pageCount}`; img.src = url; img.className = 'dr-page-image';
          source.replaceChildren(img); if(sourceURL) URL.revokeObjectURL(sourceURL); sourceURL = url;
          currentPage = number; $('[data-page]').textContent = `Page ${number} / ${pageCount}`;
          $('[data-prev]').disabled = number <= 1; $('[data-next]').disabled = number >= pageCount; pager.hidden = false;
        } catch(e) { if(!request.signal.aborted && epoch===generation && selected===selection) detail.textContent=e.message; }
      }
      $('[data-prev]').onclick=()=>void showPage(currentPage-1);
      $('[data-next]').onclick=()=>void showPage(currentPage+1);
      $('[data-choose]').onclick = () => {
        try { if(fileInput.showPicker) fileInput.showPicker(); else fileInput.click(); }
        catch { fileInput.click(); }
      };
      async function selectFile(file) {
        if (root.dataset.busy === 'true') {detail.textContent='Wait for the current document to finish before opening another.';return;}
        resetPreview(); result.textContent = ''; save.disabled = true; convert.disabled = true;
        if (!file) return;
        const epoch = generation, selected = selection;
        if (!allowed || !(await scopeOK()) || epoch !== generation || selected !== selection) {detail.textContent='Reader is not connected. Reconnect, then select the document again.';return;}
        if (!/\.(pdf|docx|xlsx|pptx|csv)$/i.test(file.name)) {detail.textContent='Supported formats: PDF, Word, Excel, PowerPoint and CSV.';return;}
        if (!maxBytes || file.size > maxBytes) {detail.textContent=`This file is ${(file.size/1048576).toFixed(1)} MiB. Current limit: ${(maxBytes/1048576).toFixed(0)} MiB.`;sourceMessage(detail.textContent);return;}
        selectedFile=file; $('[data-filename]').textContent=file.name;
        sourceMessage('Uploading document for preview…'); detail.textContent=`Uploading ${file.name} · ${(file.size/1048576).toFixed(1)} MiB`;
        const request = new AbortController(); previewRequest=request;
        try {
          const response=await fetch(`${BASE}/upload?name=${encodeURIComponent(file.name)}&preview=1`,{method:'POST',headers:{Authorization:`Bearer ${token}`},body:file,signal:request.signal});
          const data=await response.json();
          if(disposed || epoch!==generation || selected!==selection) return;
          if(!response.ok) throw new Error(data.error?.message || `Upload: HTTP ${response.status}`);
          stagedId=data.job.id; convert.disabled=false;
          if(/\.pdf$/i.test(file.name)) {
            const pages=await fetch(`${BASE}/jobs/${stagedId}/pages`,{headers:{Authorization:`Bearer ${token}`},signal:request.signal});
            if(!pages.ok) throw new Error(`Cannot preview this PDF (HTTP ${pages.status}). Check whether it is encrypted or damaged.`);
            const info=await pages.json(); if(disposed || epoch!==generation || selected!==selection) return;
            pageCount=info.pages; await showPage(1);
          } else if(/\.csv$/i.test(file.name)) {
            const text=await file.slice(0,250000).text(); if(disposed || epoch!==generation || selected!==selection) return;
            const pre=document.createElement('pre');pre.textContent=text;source.replaceChildren(pre);
          } else sourceMessage('Document uploaded. Page preview is available for PDFs; select Read document to extract this file.');
          if(epoch===generation && selected===selection) detail.textContent=`Ready to read${pageCount ? ` · ${pageCount} pages` : ''} · ${(file.size/1048576).toFixed(1)} MiB · uploaded once`;
        } catch(e) {if(!request.signal.aborted && epoch===generation && selected===selection){detail.textContent=e.message;sourceMessage(e.message);}}
      }
      fileInput.onchange=()=>void selectFile(fileInput.files[0]);
      const fileDrag=e=>Array.from(e.dataTransfer?.types || []).includes('Files');
      const dragEnter=e=>{if(!fileDrag(e))return;e.preventDefault();e.stopPropagation();root.classList.add('dr-dragover');};
      const dragOver=e=>{if(!fileDrag(e))return;e.preventDefault();e.stopPropagation();e.dataTransfer.dropEffect=root.dataset.busy==='true'?'none':'copy';};
      const dragLeave=e=>{if(!root.contains(e.relatedTarget))root.classList.remove('dr-dragover');};
      const drop=e=>{if(!fileDrag(e))return;e.preventDefault();e.stopPropagation();root.classList.remove('dr-dragover');const files=Array.from(e.dataTransfer.files);if(files.length!==1){detail.textContent='Drop one document at a time.';return;}void selectFile(files[0]);};
      for(const [type,fn] of [['dragenter',dragEnter],['dragover',dragOver],['dragleave',dragLeave],['drop',drop]]) {root.addEventListener(type,fn,true);unsubs.push(()=>root.removeEventListener(type,fn,true));}
      function clear() { resetPreview(); busy(false); generation++; allowed = false; token = ''; active?.abort(); active = null; result.textContent = ''; detail.textContent = ''; fileInput.value = ''; convert.disabled = true; save.disabled = true; }
      async function scopeOK() {
        const owner = host.state.focusedSessionOwner?.get();
        const profile = owner?.profile || host.state.profile.get();
        if (profile !== 'default') return false;
        // Fail OPEN on route-list errors: profileRoutes() enumerates every
        // registry connection (can take ~10s) and can transiently fail;
        // health + identity checks still gate.
        let routes; try { routes = await host.profileRoutes(); } catch { return true; }
        const conn = owner?.connectionId || host.state.connectionId?.get() || 'local';
        // The reader server only ever runs on this PC: only a focused session
        // on the LOCAL route (mode 'local', default profile) may use it.
        const match = routes.find(r => r.connectionId === conn);
        if (!match || match.mode !== 'local' || match.profile !== 'default') return false;
        return true;
      }
      async function connect() {
        clear(); const epoch = generation;
        status.textContent = 'Checking local AnyDoc reader…';
        try {
          if (!(await scopeOK())) throw new Error('Reader is local to this PC and its default profile. Switch back to the local default profile.');
          const response = await fetch(`${BASE}/health`, {headers:{Authorization:`Bearer ${TOKEN}`}, signal: AbortSignal.timeout(12000)});
          if (!response.ok) throw new Error(`Reader health: HTTP ${response.status}`);
          const data = await response.json();
          if (disposed || epoch !== generation) return;
          if (!data.ok) throw new Error(data.error || 'AnyDoc is unavailable');
          if (data.service !== 'anydoc-desktop-reader' || data.profile !== 'default') throw new Error('Reader identity mismatch');
          token = TOKEN; allowed = true; maxBytes = data.max_upload_bytes; convert.disabled = true;
          status.textContent = `Ready · drop or open a document · up to ${(maxBytes/1048576).toFixed(0)} MiB · local network only`;
        } catch (e) { if (!disposed && epoch === generation) status.textContent = `Not ready: ${e.message}`; }
      }
      convert.onclick = async () => {
        const file = selectedFile;
        if (!file) { detail.textContent = 'Choose a document first.'; return; }
        if (!stagedId || file.size > maxBytes) { detail.textContent = 'Wait for the upload to finish, or choose a file within the displayed limit.'; return; }
        const epoch = generation;
        if (!allowed || !(await scopeOK()) || epoch !== generation) return;
        busy(true); convert.disabled = true; save.disabled = true; result.textContent = '';
        detail.textContent = `Reading ${file.name}… scanned documents may take a few minutes.`;
        active = new AbortController();
        try {
          const headers = {Authorization:`Bearer ${token}`};
          const signal = active.signal;
          const response = await fetch(`${BASE}/jobs/${stagedId}/start`, {method:'POST', headers, signal});
          const data = await response.json();
          if (epoch !== generation || disposed) return;
          if (!response.ok) throw new Error(data.error?.message || `HTTP ${response.status}`);
          const id = data.job.id;
          const deadline = Date.now() + 1600000;
          while (Date.now() < deadline) {
            if (epoch !== generation || disposed) return;
            const poll = await fetch(`${BASE}/jobs`, {headers, signal});
            if (!poll.ok) throw new Error(`Status: HTTP ${poll.status}`);
            const job = (await poll.json()).jobs.find(j => j.id === id);
            if (epoch !== generation || disposed) return;
            if (!job) throw new Error('Conversion job was not found');
            if (job.status === 'failed') throw new Error(job.error || 'Conversion failed');
            if (job.status === 'completed') {
              const output = await fetch(`${BASE}/jobs/${id}/text`, {headers, signal});
              if (!output.ok) throw new Error(`Output: HTTP ${output.status}`);
              const markdown = await output.text();
              if (epoch !== generation || disposed) return;
              result.textContent = markdown;
              detail.textContent = `Read ${file.name} · ${markdown.length.toLocaleString()} characters`;
              save.disabled = false;
              return;
            }
            await new Promise(resolve => setTimeout(resolve, 1000));
          }
          throw new Error('Conversion timed out. The local job may still be running.');
        } catch (e) { if (!disposed && epoch === generation) detail.textContent = `Read failed: ${e.message}`; }
        finally { if (!disposed && epoch === generation) { busy(false); convert.disabled = !allowed; active = null; } }
      };
      save.onclick = () => {
        if (!allowed || !result.textContent) return;
        const a = document.createElement('a'); const url = URL.createObjectURL(new Blob([result.textContent], {type:'text/markdown;charset=utf-8'}));
        a.href = url; a.download = (selectedFile?.name || 'document').replace(/\.[^.]+$/, '') + '.md'; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      };
      $('[data-health]').onclick = connect;
      for (const key of ['profile','connectionId','focusedSessionOwner']) {
        const atom = host.state[key]; if (atom?.listen) unsubs.push(atom.listen(() => { void connect(); }));
      }
      void connect();
      return () => { disposed = true; clear(); unsubs.forEach(fn => fn()); root.remove(); };
}

function Reader() {
  const ref = React.useRef(null);
  React.useEffect(() => mountReader(ref.current), []);
  return jsx('div', {ref, style: {height:'100%', overflow:'auto'}});
}

export default {
  id: 'document-reader', version: '0.2.2', name: 'Document Reader',
  description: 'Local Firecrawl AnyDoc document reader', defaultEnabled: false,
  register(ctx) {
    ctx.register({id:'page', area:ROUTES_AREA, data:{path:'/document-reader'}, render:()=>jsx(Reader,{})});
    ctx.register({id:'nav', area:SIDEBAR_NAV_AREA, data:{path:'/document-reader',label:'Document Reader',codicon:'file-pdf'}});
    ctx.register({id:'open', area:PALETTE_AREA, data:{id:'document-reader.open',label:'Document Reader: Open',keywords:['ocr','pdf','document','anydoc'],run:()=>host.navigate('/document-reader')}});
  }
};
