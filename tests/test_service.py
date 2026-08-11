import http.client
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ocr_service", ROOT / "service" / "ocr_service.py")
service = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = service
SPEC.loader.exec_module(service)


class ServiceHelpersTest(unittest.TestCase):
    def test_service_ui_is_profile_explicit_bounded_and_keyboard_accessible(self):
        source = (ROOT / 'service' / 'firm.html').read_text(encoding='utf-8')
        self.assertIn('Hermes Document Reader', source)
        self.assertNotIn('Bearden', source)
        self.assertNotIn('OCR-Inbox', source)
        self.assertIn("$('profileName').textContent", source)
        self.assertIn('MAX_FILES = 10', source)
        self.assertIn('MAX_FILE_BYTES = 100 * 1024 * 1024', source)
        self.assertIn('if (!response.ok)', source)
        self.assertIn("document.createElement('button')", source)
        self.assertIn("setAttribute('aria-pressed'", source)
        self.assertIn('finished_with_errors', source)
        self.assertIn('Recognized text', source)

    def test_sanitize_name_strips_paths_and_windows_metacharacters(self):
        self.assertEqual(service.sanitize_name('../bad:<name>.pdf'), 'bad__name_.pdf')

    def test_sanitize_ocr_html_removes_executable_content(self):
        dirty = '''<div onclick="steal()"><script>alert(1)</script><a href="javascript:alert(2)">ok</a><img src="page.jpg" onerror="steal()"></div>'''
        clean = service.sanitize_ocr_html(dirty)
        self.assertNotIn('<script', clean)
        self.assertNotIn('onclick', clean)
        self.assertNotIn('onerror', clean)
        self.assertNotIn('javascript:', clean)
        self.assertNotIn('src=', clean)

    def test_spreadsheet_formula_text_is_neutralized(self):
        value = service.safe_spreadsheet_text('=HYPERLINK("https://example.invalid","open")')
        self.assertTrue(value.startswith("'="))

    def test_extract_regions_maps_normalized_boxes_and_kinds(self):
        raw = '''
          <div data-label="Section-Header" data-bbox="100 50 900 120">Heading</div>
          <div class="Text" data-bbox="125 150 500 260">Words</div>
          <div class="Table" data-bbox="100 300 900 800"><table></table></div>
          <div data-label="Text" data-bbox="bad box">ignored</div>
        '''
        regions = service.extract_regions(raw)
        self.assertEqual([r['kind'] for r in regions], ['section', 'text', 'data'])
        self.assertEqual(regions[0], {
            'x': 10.0, 'y': 5.0, 'w': 80.0, 'h': 7.0,
            'kind': 'section', 'label': 'Section-Header',
        })

    def test_extract_regions_retains_structural_boxes_at_live_limit(self):
        blocks = ['<div data-label="Section-Header" data-bbox="0 0 1000 30">H</div>']
        blocks.extend(
            f'<div data-label="Text" data-bbox="0 {i} 1000 {i + 1}">T</div>'
            for i in range(1, 61)
        )
        regions = service.extract_regions(''.join(blocks), limit=10)
        self.assertEqual(len(regions), 10)
        self.assertEqual(regions[0]['kind'], 'section')


class HttpHandlerTest(unittest.TestCase):
    TOKEN = 'A' * 64
    OWNER = 'a' * 64
    INSTANCE = 'b' * 32

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self.tmp.name) / 'inbox'
        self.jobs = Path(self.tmp.name) / 'jobs'
        self.inbox.mkdir()
        self.jobs.mkdir()
        self.old_jobs = service.JOBS_DIR
        service.JOBS_DIR = self.jobs
        self.server = service.build_server(
            '127.0.0.1', 0,
            auth_token=self.TOKEN,
            profile='default',
            data_root=Path(self.tmp.name),
            owner_fingerprint=self.OWNER,
            instance_id=self.INSTANCE,
            runtime_identity=None,
        )
        self.server.inbox = self.inbox
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        service.JOBS_DIR = self.old_jobs
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        request_headers = {
            'X-Document-Reader-Token': self.TOKEN,
            'X-Document-Reader-Owner': self.OWNER,
        }
        request_headers.update(headers or {})
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        conn.request(method, path, body=body, headers=request_headers)
        response = conn.getresponse()
        payload = response.read()
        result = response.status, dict(response.getheaders()), payload
        conn.close()
        return result

    def test_upload_is_committed_under_final_name(self):
        status, headers, body = self.request(
            'POST', '/api/upload?name=scan.pdf', b'%PDF-test',
            {'Content-Length': '9'},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'ok': True, 'name': 'scan.pdf'})
        self.assertEqual((self.inbox / 'scan.pdf').read_bytes(), b'%PDF-test')
        self.assertEqual(list(self.inbox.glob('*.uploading')), [])
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')

    def test_cross_site_upload_is_rejected(self):
        status, _, _ = self.request(
            'POST', '/api/upload?name=scan.pdf', b'x',
            {'Content-Length': '1', 'Origin': 'https://example.test'},
        )
        self.assertEqual(status, 403)
        self.assertFalse((self.inbox / 'scan.pdf').exists())

    def test_state_requires_authentication(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        conn.request('GET', '/api/state')
        response = conn.getresponse()
        response.read()
        self.assertEqual(response.status, 401)
        conn.close()

    def test_jobs_path_cannot_escape_jobs_directory(self):
        sibling = self.jobs.parent / (self.jobs.name + '-private')
        sibling.mkdir()
        (sibling / 'secret.txt').write_text('secret', encoding='utf-8')
        status, _, _ = self.request('GET', f'/jobs/../{sibling.name}/secret.txt')
        self.assertEqual(status, 400)

    def test_served_job_html_is_sanitized(self):
        job_id = '20260810-010101-aaaaaaaa'
        job = self.jobs / job_id
        job.mkdir()
        (job / 'page_1.html').write_text(
            '<p onclick="bad()">safe</p><script>bad()</script>', encoding='utf-8'
        )
        status, _, body = self.request('GET', f'/jobs/{job_id}/page_1.html')
        self.assertEqual(status, 200)
        rendered = body.decode('utf-8')
        self.assertIn('<p>safe</p>', rendered)
        self.assertNotIn('onclick', rendered)
        self.assertNotIn('<script', rendered)

    def test_job_file_with_url_encoded_spaces_downloads(self):
        job_id = '20260810-010102-bbbbbbbb'
        job = self.jobs / job_id
        job.mkdir()
        (job / 'finished scan.txt').write_text('done', encoding='utf-8')
        status, headers, body = self.request(
            'GET', f'/jobs/{job_id}/finished%20scan.txt'
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b'done')
        self.assertIn('attachment', headers['Content-Disposition'])


if __name__ == '__main__':
    unittest.main()
