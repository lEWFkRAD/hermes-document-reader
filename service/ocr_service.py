# -*- coding: utf-8 -*-
"""
Bearden Firm OCR Service — watched-folder OCR with a live web UI.

Staff usage (no install):
  1. Drop a scanned PDF into \\\\SHATNER\\M\\OCR-Inbox  (D:\\OCR-Inbox locally),
     or drag it onto the web page.
  2. Watch it scan at http://shatner:8899/ (LAN/tailnet).
  3. Outputs land in OCR-Inbox\\Processed: <name>.md and <name>.xlsx,
     alongside the original (moved there when done).

Engine: anydoc page loading + GRM (grm-2.6-plus-0628) on the forge 5090 via
scripts/grm_ocr.py (thinking disabled, repeat retry, output normalization).

Run under the Hermes venv python:
  venv\\Scripts\\python.exe ocr_service.py [--port 8899] [--inbox D:\\OCR-Inbox]

All live job state is kept in memory and served as JSON (/api/state) — no
status-file writes, which kills the whole Windows file-lock class the
single-shot viewer had to retry around.
"""

import argparse
import io
import json
import re
import shutil
import sys
import threading
import time
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bs4 import BeautifulSoup
from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).parent.parent))

from chandra.input import load_file

import grm_ocr

VIEWER_DIR = Path(__file__).parent
STATE_DIR = VIEWER_DIR / "service"
JOBS_DIR = STATE_DIR / "jobs"
HISTORY_PATH = STATE_DIR / "history.json"
DISPLAY_WIDTH = 1100
SUPPORTED = {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".bmp"}

LOCK = threading.Lock()
STATE = {
    "queue": [],       # [{name, size}] waiting
    "job": None,       # live job dict (same shape the viewer UI expects) + partial
    "history": [],     # [{name, when, pages, secs, chars, links:{md,xlsx}, errors}]
}
_pending_paths = []    # Path objects matching STATE["queue"]


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_history():
    if HISTORY_PATH.exists():
        try:
            STATE["history"] = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        except Exception:
            STATE["history"] = []


def save_history():
    HISTORY_PATH.write_text(
        json.dumps(STATE["history"], indent=1), encoding="utf-8"
    )


def sanitize_name(name: str) -> str:
    name = Path(name).name
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip() or "upload.pdf"


def unique_path(directory: Path, name: str) -> Path:
    p = directory / name
    stem, suffix = p.stem, p.suffix
    k = 1
    while p.exists():
        p = directory / f"{stem} ({k}){suffix}"
        k += 1
    return p


# ---------------------------------------------------------------- excel export

def _table_rows(tbl) -> list:
    rows = []
    for tr in tbl.find_all("tr"):
        rows.append([c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])])
    return rows


def export_xlsx(page_htmls: list, out_path: Path) -> None:
    """Tables from every page → one sheet per unique table; text → a Text sheet.

    Scanned tax forms often carry several identical copies of the same form on
    one page (W-2 Copy B/C/2…) — identical tables are deduplicated so the
    workbook isn't a wall of clones. Trivial 1-cell tables are skipped.
    """
    wb = Workbook()
    text_ws = wb.active
    text_ws.title = "Text"
    text_ws.column_dimensions["A"].width = 110
    row = 1
    seen = set()
    for page_num, html in enumerate(page_htmls, 1):
        soup = BeautifulSoup(html or "", "html.parser")
        page_tables = 0
        dupes = 0
        for tbl in soup.find_all("table"):
            rows = _table_rows(tbl)
            tbl.decompose()
            cells = [c for r in rows for c in r if c]
            if len(cells) < 2:
                continue
            key = repr(rows)
            if key in seen:
                dupes += 1
                continue
            seen.add(key)
            page_tables += 1
            ws = wb.create_sheet(f"P{page_num} Table {page_tables}"[:31])
            widths = {}
            for r, cols in enumerate(rows, 1):
                for c, val in enumerate(cols, 1):
                    num = val.replace(",", "").replace("$", "").strip()
                    if re.fullmatch(r"-?\d+(\.\d+)?", num or "x"):
                        ws.cell(row=r, column=c, value=float(num))
                    else:
                        ws.cell(row=r, column=c, value=val)
                    widths[c] = min(60, max(widths.get(c, 10), len(val) + 2))
            for c, w in widths.items():
                ws.column_dimensions[ws.cell(row=1, column=c).column_letter].width = w
        text = soup.get_text("\n", strip=True)
        header = f"--- Page {page_num} ---" + (
            f"  ({dupes} duplicate form copy(ies) omitted)" if dupes else ""
        )
        text_ws.cell(row=row, column=1, value=header)
        row += 1
        for line in text.splitlines():
            if line.strip():
                text_ws.cell(row=row, column=1, value=line.strip())
                row += 1
        row += 1
    wb.save(out_path)


def export_txt(page_htmls: list) -> str:
    """Readable plain text: document order preserved, tables as aligned rows,
    duplicate form copies noted once — no raw HTML."""
    out = []
    seen = set()
    for page_num, html in enumerate(page_htmls, 1):
        out.append(f"========== Page {page_num} ==========\n")
        soup = BeautifulSoup(html or "", "html.parser")
        for tbl in soup.find_all("table"):
            rows = _table_rows(tbl)
            key = repr(rows)
            if key in seen:
                tbl.replace_with(soup.new_string("[duplicate copy of the table above omitted]\n"))
                continue
            seen.add(key)
            widths = {}
            for cols in rows:
                for i, v in enumerate(cols):
                    widths[i] = min(44, max(widths.get(i, 0), len(v)))
            lines = []
            for cols in rows:
                if not any(c.strip() for c in cols):
                    continue
                lines.append("  ".join(v.ljust(widths[i]) for i, v in enumerate(cols)).rstrip())
            tbl.replace_with(soup.new_string("\n" + "\n".join(lines) + "\n"))
        text = soup.get_text("\n")
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        out.append(text + "\n")
    return "\n".join(out)


# ---------------------------------------------------------------- job worker

def process_file(src: Path, inbox: Path) -> None:
    processed = inbox / "Processed"
    processed.mkdir(exist_ok=True)
    job_id = time.strftime("%Y%m%d-%H%M%S")
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    job = {
        "id": job_id,
        "name": src.name,
        "current_file": src.name,
        "state": "loading",
        "total": 0,
        "done": 0,
        "current": 0,
        "started": time.time(),
        "pages": [],
        "partial": "",
        "error": None,
    }
    with LOCK:
        STATE["job"] = job

    started = time.time()
    page_htmls = []
    page_mds = []
    try:
        images = load_file(str(src), {})
        job["total"] = len(images)
        job["pages"] = [
            {"n": i + 1, "file": src.name, "state": "pending", "secs": None, "chars": None}
            for i in range(len(images))
        ]
        job["state"] = "rendering"
        for i, img in enumerate(images):
            disp = img
            if img.width > DISPLAY_WIDTH:
                disp = img.resize((DISPLAY_WIDTH, int(img.height * DISPLAY_WIDTH / img.width)))
            disp.convert("RGB").save(job_dir / f"page_{i + 1}.jpg", quality=82)

        job["state"] = "ocr"
        for i, img in enumerate(images):
            page = job["pages"][i]
            page["state"] = "working"
            job["current"] = i + 1
            job["partial"] = ""
            t0 = time.time()
            last = [0.0]

            def on_delta(raw_so_far):
                now = time.time()
                if now - last[0] < 0.4:
                    return
                last[0] = now
                try:
                    md = grm_ocr.raw_to_markdown(raw_so_far)
                except Exception:
                    md = ""
                if not md or md.lstrip().startswith(("<!DOCTYPE", "<html", "<script")):
                    md = BeautifulSoup(
                        grm_ocr.normalize_raw(raw_so_far), "html.parser"
                    ).get_text("\n")
                job["partial"] = md

            try:
                raw = grm_ocr.ocr_page_raw(img, on_delta=on_delta)
                md = grm_ocr.raw_to_markdown(raw)
                html = grm_ocr.raw_to_html(raw)
                (job_dir / f"page_{i + 1}.md").write_text(md, encoding="utf-8")
                (job_dir / f"page_{i + 1}.html").write_text(html, encoding="utf-8")
                page_mds.append(md)
                page_htmls.append(html)
                page["state"] = "done"
                page["secs"] = round(time.time() - t0, 1)
                page["chars"] = len(md)
                job["done"] += 1
            except Exception as e:
                page["state"] = "error"
                page["secs"] = round(time.time() - t0, 1)
                page["error"] = str(e)[:300]
                page_mds.append("")
                page_htmls.append("")
            job["partial"] = ""

        # outputs: readable text + excel, into Processed and the job dir (UI links)
        stem = src.stem
        txt_out = unique_path(processed, f"{stem}.txt")
        txt_out.write_text(export_txt(page_htmls), encoding="utf-8")
        xlsx_out = unique_path(processed, f"{stem}.xlsx")
        try:
            export_xlsx(page_htmls, xlsx_out)
        except Exception as e:
            log(f"xlsx export failed for {src.name}: {e}")
            xlsx_out = None
        shutil.copy(txt_out, job_dir / txt_out.name)
        if xlsx_out:
            shutil.copy(xlsx_out, job_dir / xlsx_out.name)

        # move the original out of the inbox so it never reprocesses
        dest = unique_path(processed, src.name)
        for _ in range(10):
            try:
                shutil.move(str(src), str(dest))
                break
            except OSError:
                time.sleep(0.5)

        errors = sum(1 for p in job["pages"] if p["state"] != "done")
        job["state"] = "finished"
        entry = {
            "id": job_id,
            "name": src.name,
            "when": time.strftime("%Y-%m-%d %H:%M"),
            "pages": job["total"],
            "errors": errors,
            "secs": round(time.time() - started, 0),
            "chars": sum(p["chars"] or 0 for p in job["pages"]),
            "links": {
                "md": f"/jobs/{job_id}/{txt_out.name}",
                "xlsx": f"/jobs/{job_id}/{xlsx_out.name}" if xlsx_out else None,
            },
            "paths": {
                "folder": str(processed),
                "txt": str(txt_out),
                "xlsx": str(xlsx_out) if xlsx_out else None,
                "original": str(dest),
            },
        }
        with LOCK:
            STATE["history"].insert(0, entry)
            STATE["history"] = STATE["history"][:200]
            save_history()
        log(f"done: {src.name} ({job['total']} pages, {errors} errors)")
    except Exception as e:
        job["state"] = "failed"
        job["error"] = str(e)[:500]
        log(f"FAILED: {src.name}: {e}")
    finally:
        time.sleep(2)  # let the UI show the finished state before clearing
        with LOCK:
            if STATE["job"] is job:
                STATE["job"] = None


def worker(inbox: Path):
    while True:
        src = None
        with LOCK:
            if _pending_paths:
                src = _pending_paths.pop(0)
                STATE["queue"].pop(0)
        if src is None:
            time.sleep(1)
            continue
        if src.exists():
            process_file(src, inbox)


def watcher(inbox: Path):
    """Enqueue new files once their size is stable (copy finished)."""
    sizes = {}
    while True:
        try:
            for p in sorted(inbox.iterdir()):
                if p.is_dir() or p.suffix.lower() not in SUPPORTED:
                    continue
                with LOCK:
                    queued = any(q == p for q in _pending_paths) or (
                        STATE["job"] and STATE["job"]["name"] == p.name
                        and STATE["job"]["state"] not in ("finished", "failed")
                    )
                if queued:
                    continue
                size = p.stat().st_size
                if sizes.get(p) == size and size > 0:
                    with LOCK:
                        _pending_paths.append(p)
                        STATE["queue"].append({"name": p.name, "size": size})
                    log(f"queued: {p.name}")
                    sizes.pop(p, None)
                else:
                    sizes[p] = size
        except Exception as e:
            log(f"watcher error: {e}")
        time.sleep(2)


# ---------------------------------------------------------------- http server

class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/state":
            with LOCK:
                snap = {
                    "queue": list(STATE["queue"]),
                    "job": dict(STATE["job"]) if STATE["job"] else None,
                    "history": STATE["history"][:30],
                }
            if snap["job"]:
                snap["job"]["base"] = f"/jobs/{snap['job']['id']}"
            return self._json(snap)
        if path == "/" or path == "/index.html":
            return self._file(VIEWER_DIR / "firm.html", "text/html")
        if path.startswith("/jobs/"):
            target = (JOBS_DIR / path[len("/jobs/"):]).resolve()
            if not str(target).startswith(str(JOBS_DIR.resolve())):
                return self._json({"error": "bad path"}, 400)
            ctype = {
                ".jpg": "image/jpeg", ".md": "text/markdown; charset=utf-8",
                ".txt": "text/plain; charset=utf-8",
                ".html": "text/html; charset=utf-8",
                ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            }.get(target.suffix.lower(), "application/octet-stream")
            return self._file(target, ctype)
        return self._json({"error": "not found"}, 404)

    def _file(self, p: Path, ctype: str):
        if not p.exists():
            return self._json({"error": "not found"}, 404)
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        if p.suffix.lower() in (".xlsx", ".md", ".txt") and "jobs" in str(p):
            self.send_header(
                "Content-Disposition", f'attachment; filename="{p.name}"'
            )
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path)
        if path.path != "/api/upload":
            return self._json({"error": "not found"}, 404)
        qs = urllib.parse.parse_qs(path.query)
        name = sanitize_name(qs.get("name", ["upload.pdf"])[0])
        if Path(name).suffix.lower() not in SUPPORTED:
            return self._json({"error": f"unsupported type: {name}"}, 400)
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > 500 * 1024 * 1024:
            return self._json({"error": "bad size"}, 400)
        dest = unique_path(self.server.inbox, name)
        with open(dest, "wb") as f:
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        log(f"uploaded: {dest.name}")
        return self._json({"ok": True, "name": dest.name})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--inbox", default=r"D:\OCR-Inbox")
    ap.add_argument("--bind", default="0.0.0.0")
    args = ap.parse_args()

    inbox = Path(args.inbox)
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "Processed").mkdir(exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    load_history()

    threading.Thread(target=watcher, args=(inbox,), daemon=True).start()
    threading.Thread(target=worker, args=(inbox,), daemon=True).start()

    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    server.inbox = inbox
    log(f"Bearden OCR service on http://{args.bind}:{args.port}/  inbox={inbox}")
    server.serve_forever()


if __name__ == "__main__":
    main()
