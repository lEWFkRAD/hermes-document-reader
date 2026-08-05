# -*- coding: utf-8 -*-
"""
Shared GRM OCR client for the Bearden/Onyx stack.

Sends a page image to GRM (grm-2.6-plus-0628) on the forge 5090 with thinking
DISABLED (GRM is a reasoner; without enable_thinking=false it burns its token
budget in think blocks and returns empty output — same failure mode as the old
Muse compile_knowledge bug). Uses chandra's prompt + parsers, but its own
OpenAI client so we control extra_body and can stream token deltas.

Used by:
  - scripts/anydoc-mcp.py           (MCP tool convert_with_ocr)
  - scripts/anydoc-ocr-viewer/      (live side-by-side viewer)
"""

import base64
import io
import os
import re

from bs4 import BeautifulSoup
from openai import OpenAI

from chandra.model.util import detect_repeat_token, scale_to_fit
from chandra.output import parse_html, parse_markdown
from chandra.prompts import PROMPT_MAPPING

API_BASE = os.environ.get("GRM_OCR_API_BASE", "http://your-vllm-host:8000/v1")
API_KEY = os.environ.get("GRM_OCR_API_KEY", "local")
MODEL = os.environ.get("GRM_OCR_MODEL", "grm-2.6-plus-0628")
MAX_TOKENS = int(os.environ.get("GRM_OCR_MAX_TOKENS", "8192"))
REQUEST_TIMEOUT = int(os.environ.get("GRM_OCR_TIMEOUT", "300"))

_client = None


def client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=API_KEY, base_url=API_BASE, timeout=REQUEST_TIMEOUT)
    return _client


def _stream_once(b64: str, on_delta, temperature: float, top_p: float):
    """One streamed completion. Returns (raw, aborted_for_repeats)."""
    stream = client().chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "text", "text": PROMPT_MAPPING["ocr_layout"]},
                ],
            }
        ],
        max_tokens=MAX_TOKENS,
        temperature=temperature,
        top_p=top_p,
        stream=True,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    parts = []
    size = 0
    next_check = 1500
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta.content
        if not delta:
            continue
        parts.append(delta)
        size += len(delta)
        if on_delta is not None:
            on_delta("".join(parts))
        if size >= next_check:
            next_check = size + 1500
            if detect_repeat_token("".join(parts)):
                try:
                    stream.close()
                except Exception:
                    pass
                return "".join(parts), True
    return "".join(parts), False


_IMG_MD = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def _mostly_images(raw: str) -> bool:
    """True when the page parsed to little besides image placeholders.

    GRM occasionally classifies a form-dense page (e.g. four W-2 copies) as
    Figure regions and reads nothing — a nondeterministic miss worth a retry.
    Pages that legitimately are pure images just burn the retries and keep
    the last answer.
    """
    try:
        md = raw_to_markdown(raw)
    except Exception:
        return False
    text = _IMG_MD.sub("", md).strip()
    return len(md) > 0 and len(text) < 120


def ocr_page_raw(image, on_delta=None, max_retries: int = 3) -> str:
    """OCR one PIL page image. Returns chandra-format raw output.

    on_delta(text_so_far) is called after each streamed chunk when given (on a
    retry it restarts from empty). Degenerate repetition (e.g. multi-copy 1099
    pages sending the model into a loop) is detected mid-stream, the request is
    aborted, and generation retries with bumped temperature — same policy as
    chandra's generate_vllm.
    """
    img = scale_to_fit(image)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

    temperature, top_p = 0.0, 0.1
    raw = ""
    for attempt in range(max_retries + 1):
        raw, aborted = _stream_once(b64, on_delta, temperature, top_p)
        bad = aborted or detect_repeat_token(raw) or (
            len(raw) > 50 and detect_repeat_token(raw, cut_from_end=50)
        ) or _mostly_images(raw)
        if not bad or attempt == max_retries:
            break
        temperature = min(0.2 * (attempt + 1), 0.8)
        top_p = 0.95
    return raw


def normalize_raw(raw: str) -> str:
    """GRM wraps its answer in a ```html fence and an <html><body> shell;
    chandra's parsers expect bare top-level divs. Unwrap both."""
    text = raw.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    if "<body" in text:
        soup = BeautifulSoup(text, "html.parser")
        body = soup.find("body")
        if body is not None:
            text = "".join(str(c) for c in body.children)
    return text


def raw_to_markdown(raw: str) -> str:
    return parse_markdown(normalize_raw(raw))


def raw_to_html(raw: str) -> str:
    return parse_html(normalize_raw(raw))


def ocr_page_markdown(image) -> str:
    return raw_to_markdown(ocr_page_raw(image))
