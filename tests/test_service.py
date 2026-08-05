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
    def test_sanitize_name_strips_paths_and_windows_metacharacters(self):
        self.assertEqual(service.sanitize_name('../bad:<name>.pdf'), 'bad__name_.pdf')

    def test_sanitize_ocr_html_removes_executable_content(self):
        dirty = '''<div onclick="steal()"><script>alert(1)</script><a href="javascript:alert(2)">ok</a><img src="page.jpg" onerror="steal()"></div>'''
        clean = service.sanitize_ocr_html(dirty)
        self.assertNotIn('<script', clean)
        self.assertNotIn('onclick', clean)
        self.assertNotIn('onerror', clean)
        self.assertNotIn('javascript:', clean)
        self.assertIn('src="page.jpg"', clean)


class HttpHandlerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self.tmp.name) / 'inbox'
        self.jobs = Path(self.tmp.name) / 'jobs'
        self.inbox.mkdir()
        self.jobs.mkdir()
        self.old_jobs = service.JOBS_DIR
        service.JOBS_DIR = self.jobs
        self.server = service.ThreadingHTTPServer(('127.0.0.1', 0), service.Handler)
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
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        conn.request(method, path, body=body, headers=headers or {})
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

    def test_jobs_path_cannot_escape_jobs_directory(self):
        sibling = self.jobs.parent / (self.jobs.name + '-private')
        sibling.mkdir()
        (sibling / 'secret.txt').write_text('secret', encoding='utf-8')
        status, _, _ = self.request('GET', f'/jobs/../{sibling.name}/secret.txt')
        self.assertEqual(status, 400)

    def test_served_job_html_is_sanitized(self):
        job = self.jobs / 'job-1'
        job.mkdir()
        (job / 'page_1.html').write_text(
            '<p onclick="bad()">safe</p><script>bad()</script>', encoding='utf-8'
        )
        status, _, body = self.request('GET', '/jobs/job-1/page_1.html')
        self.assertEqual(status, 200)
        rendered = body.decode('utf-8')
        self.assertIn('<p>safe</p>', rendered)
        self.assertNotIn('onclick', rendered)
        self.assertNotIn('<script', rendered)

    def test_job_file_with_url_encoded_spaces_downloads(self):
        job = self.jobs / 'job-2'
        job.mkdir()
        (job / 'finished scan.txt').write_text('done', encoding='utf-8')
        status, headers, body = self.request('GET', '/jobs/job-2/finished%20scan.txt')
        self.assertEqual(status, 200)
        self.assertEqual(body, b'done')
        self.assertIn('attachment', headers['Content-Disposition'])


if __name__ == '__main__':
    unittest.main()
