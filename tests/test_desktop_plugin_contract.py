from pathlib import Path
import re


PLUGIN = (
    Path(__file__).resolve().parents[1]
    / "desktop-plugin"
    / "document-reader"
    / "plugin.js"
)


def test_desktop_uses_only_owned_proxy_and_profile_keyed_cache():
    source = PLUGIN.read_text(encoding="utf-8")
    assert "ctx.rest(" in source
    assert re.search(r"\bfetch\(", source) is None
    assert "127.0.0.1" not in source
    assert "service.token" not in source
    assert "queryKey: [ID, profile" in source
    assert "queryClient.removeQueries({ queryKey: [ID, previous.current] })" in source
    assert "gcTime: 15000" in source
    assert "queryKey: [ID, profile, 'asset', previousJob.current]" in source
    assert "dangerouslySetInnerHTML" not in source
    assert "DOMParser" in source and "textContent" in source


def test_hud_attachment_contract_is_bounded_cancel_safe_and_receipts_after_upload():
    source = PLUGIN.read_text(encoding="utf-8")
    assert "area: COMPOSER_AREAS.attachments" in source
    assert "run: ({ insertText }) => hudUpload(ctx, insertText)" in source
    assert "document.createElement('input')" in source
    assert "input.removeEventListener('change', onChange)" in source
    assert "input.removeEventListener('cancel', onCancel)" in source
    assert "window.removeEventListener('focus', onFocus)" in source
    assert "input.remove()" in source
    assert "MAX_FILES = 10" in source
    assert "MAX_FILE_BYTES = 100 * 1024 * 1024" in source
    assert "file.arrayBuffer()" in source
    assert "upload: { filename: file.name, contentType: MIME[ext], bytes }" in source
    assert source.index("await uploadFiles(ctx, files)") < source.index("insertText(`")
    assert "names.join" not in source
    assert "[Document Reader queued ${count} file" in source
    assert "Document Reader could not finish the batch; check its queue" in source
    hud = source[source.index("async function hudUpload"):source.index("function useProfileReset")]
    assert "error?.message" not in hud


def test_upload_batch_is_bound_to_one_profile_identity():
    source = PLUGIN.read_text(encoding="utf-8")
    assert "expected_profile=${encodeURIComponent(identity.profile)}" in source
    assert "expected_fingerprint=${encodeURIComponent(identity.profile_fingerprint)}" in source
    assert "result.profile !== identity.profile" in source
    assert "result.profile_fingerprint !== identity.profile_fingerprint" in source
    assert "profile_fingerprint: query.data?.profile_fingerprint" in source


def test_page_upload_drag_queue_and_empty_state_preserve_dirty_ux_fixes():
    source = PLUGIN.read_text(encoding="utf-8")
    for handler in (
        "onDragEnterCapture",
        "onDragOverCapture",
        "onDragLeaveCapture",
        "onDropCapture",
    ):
        assert handler in source
    assert "dragDepth.current" in source
    assert "role: 'button'" in source
    assert "tabIndex: 0" in source
    assert "event.key === 'Enter' || event.key === ' '" in source
    assert "OFFSCREEN_INPUT" in source
    assert "Queue" in source and "1 active" in source and "waiting" in source
    assert "queue.slice(0, 12)" in source
    assert "inFlight.current" in source
    assert "Reveal" not in source and "Copy path" not in source


def test_descriptor_exports_canonical_identity_and_version():
    source = PLUGIN.read_text(encoding="utf-8")
    assert "const ID = 'document-reader'" in source
    assert "const VERSION = '0.1.0'" in source
    assert "id: ID" in source
    assert "version: VERSION" in source
    assert "Profile-scoped Document Reader" in source
    assert "function humanJobState" in source
    assert "-p ${profile}" in source
