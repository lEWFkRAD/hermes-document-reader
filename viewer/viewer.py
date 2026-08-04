# -*- coding: utf-8 -*-
"""
Live OCR viewer — watch Chandra + GRM (forge 5090) work through a scanned PDF
page by page, with the page image and its OCR output side by side.

Usage (Hermes venv python):
  python viewer.py "D:\\path\\to\\scanned.pdf" [--port 8899]

Serves a local UI at http://localhost:<port>/ and OCRs one page at a time,
writing page_<n>.jpg / page_<n>.md / page_<n>.html plus status.json into a
per-job folder. The UI polls status.json and follows the job live.
Tailnet/local only — binds 127.0.0.1.
"""

import argparse
import json
import os
import shutil
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bs4 import BeautifulSoup
from chandra.input import load_file

import grm_ocr  # shared GRM client: thinking disabled, streaming, fence normalization

VIEWER_DIR = Path(__file__).parent
JOBS_DIR = VIEWER_DIR / "jobs"
DISPLAY_WIDTH = 1100  # px, for the browser-side page image


def run_job(pdf_paths: list, job_dir: Path) -> None:
    label = (
        pdf_paths[0].name
        if len(pdf_paths) == 1
        else f"{len(pdf_paths)} files: " + ", ".join(p.name for p in pdf_paths)
    )
    status = {
        "files": [str(p) for p in pdf_paths],
        "name": label,
        "current_file": "",
        "state": "loading",
        "total": 0,
        "done": 0,
        "current": 0,
        "started": time.time(),
        "pages": [],
        "error": None,
    }

    def save_status():
        tmp = job_dir / "status.json.tmp"
        tmp.write_text(json.dumps(status), encoding="utf-8")
        for _ in range(6):  # HTTP thread may hold status.json open mid-poll
            try:
                os.replace(tmp, job_dir / "status.json")
                return
            except OSError:
                time.sleep(0.15)

    save_status()
    try:
        images = []  # (file_name, PIL image) across all inputs, continuous numbering
        for p in pdf_paths:
            for img in load_file(str(p), {}):
                images.append((p.name, img))
        status["total"] = len(images)
        status["pages"] = [
            {"n": i + 1, "file": fname, "state": "pending", "secs": None, "chars": None}
            for i, (fname, _) in enumerate(images)
        ]
        status["state"] = "rendering"
        save_status()

        for i, (_, img) in enumerate(images):
            disp = img
            if img.width > DISPLAY_WIDTH:
                disp = img.resize(
                    (DISPLAY_WIDTH, int(img.height * DISPLAY_WIDTH / img.width))
                )
            disp.convert("RGB").save(job_dir / f"page_{i + 1}.jpg", quality=82)

        status["state"] = "ocr"
        save_status()

        for i, (fname, img) in enumerate(images):
            page = status["pages"][i]
            page["state"] = "working"
            status["current"] = i + 1
            status["current_file"] = fname
            save_status()
            t0 = time.time()
            partial_path = job_dir / f"page_{i + 1}.partial.md"
            last_partial = [0.0]

            def on_delta(raw_so_far):
                # Throttled live partial for the UI's streaming pane
                now = time.time()
                if now - last_partial[0] < 0.4:
                    return
                last_partial[0] = now
                try:
                    md = grm_ocr.raw_to_markdown(raw_so_far)
                except Exception:
                    md = ""
                # Partial raw can parse into chandra's KaTeX HTML scaffold —
                # fall back to plain text for the live pane
                if not md or md.lstrip().startswith(("<!DOCTYPE", "<html", "<script")):
                    md = BeautifulSoup(
                        grm_ocr.normalize_raw(raw_so_far), "html.parser"
                    ).get_text("\n")
                # Best-effort: the HTTP thread may hold the file open while
                # serving it (Windows lock) — skip this tick, not the page
                try:
                    tmp = job_dir / "partial.tmp"
                    tmp.write_text(md, encoding="utf-8")
                    os.replace(tmp, partial_path)
                except OSError:
                    pass

            try:
                raw = grm_ocr.ocr_page_raw(img, on_delta=on_delta)
                md = grm_ocr.raw_to_markdown(raw)
                html = grm_ocr.raw_to_html(raw)
                (job_dir / f"page_{i + 1}.md").write_text(md, encoding="utf-8")
                (job_dir / f"page_{i + 1}.html").write_text(html, encoding="utf-8")
                page["state"] = "done"
                page["secs"] = round(time.time() - t0, 1)
                page["chars"] = len(md)
                status["done"] += 1
            except Exception as e:  # keep going on per-page failures
                page["state"] = "error"
                page["secs"] = round(time.time() - t0, 1)
                page["error"] = str(e)[:300]
            finally:
                for _ in range(5):  # HTTP thread may briefly hold the file open
                    try:
                        partial_path.unlink(missing_ok=True)
                        break
                    except OSError:
                        time.sleep(0.2)
            save_status()

        # one merged markdown per input file
        by_file = {}
        for i, (fname, _) in enumerate(images):
            md = job_dir / f"page_{i + 1}.md"
            if md.exists():
                by_file.setdefault(fname, []).append(md.read_text(encoding="utf-8"))
        for fname, parts in by_file.items():
            out = job_dir / f"{Path(fname).stem}.full.md"
            out.write_text("\n\n".join(parts), encoding="utf-8")
        status["state"] = "finished"
        status["finished"] = time.time()
        save_status()
    except Exception as e:
        status["state"] = "failed"
        status["error"] = str(e)[:500]
        save_status()
        raise


class JobHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *args):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+", help="Absolute path(s) to scanned PDFs (or images)")
    ap.add_argument("--port", type=int, default=8899)
    args = ap.parse_args()

    pdf_paths = [Path(p) for p in args.pdfs]
    for p in pdf_paths:
        if not p.is_absolute() or not p.exists():
            sys.exit(f"File must exist and be absolute: {p}")

    job_dir = JOBS_DIR / time.strftime("%Y%m%d-%H%M%S")
    job_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(VIEWER_DIR / "index.html", job_dir / "index.html")

    worker = threading.Thread(target=run_job, args=(pdf_paths, job_dir), daemon=True)
    worker.start()

    os.chdir(job_dir)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), JobHandler)
    print(f"Viewer: http://localhost:{args.port}/  (job dir: {job_dir})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
