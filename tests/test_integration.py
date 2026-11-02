"""Opt-in integration test against local synthetic services and a real Chrome."""
import base64
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import network_capture as capture


@unittest.skipUnless(os.environ.get('RECORDER_INTEGRATION') == '1', 'set RECORDER_INTEGRATION=1 for real Chrome test')
class ChromeIntegration(unittest.TestCase):
    def test_multitab_http_websocket_and_files(self):
        from selenium.webdriver.chrome.options import Options
        from websockets.sync.server import serve

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"ok":true,"source":"local-synthetic-test"}' if self.path.startswith('/api') else b'<html><title>Recorder test</title><body>Local synthetic page</body></html>'
                self.send_response(200)
                self.send_header('Content-Type', 'application/json' if self.path.startswith('/api') else 'text/html')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *_):
                pass

        def echo(ws):
            for message in ws:
                ws.send(message)

        http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        ws_server = serve(echo, '127.0.0.1', 0)
        threading.Thread(target=ws_server.serve_forever, daemon=True).start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        self.addCleanup(ws_server.shutdown)
        url = f'http://127.0.0.1:{http.server_port}'
        ws_url = f'ws://127.0.0.1:{ws_server.socket.getsockname()[1]}'
        with tempfile.TemporaryDirectory() as folder:
            app = object.__new__(capture.NetworkCaptureApp)
            app._log = lambda _: None
            opts = Options()
            opts.add_argument('--headless=new')
            with patch('selenium.webdriver.chrome.options.Options', return_value=opts):
                driver = app._create_driver()
            self.addCleanup(driver.quit)
            driver.get(url)
            monitor = capture.MultiTabNetworkMonitor(app._debugger_address)
            self.addCleanup(monitor.stop)
            app._cdp_monitor = monitor
            app._filter_settings = dict(include_filter='all')
            app._redact = False
            app._jsonl_path = Path(folder) / 'requests.jsonl'
            app._capture_count = app._websocket_frame_count = app._skipped_count = 0
            app._websocket_urls = {}
            app._lock = threading.Lock()
            app._recording = threading.Event()
            monitor.start()
            app._recording.set()
            worker = threading.Thread(target=app._record_loop, daemon=True)
            worker.start()
            try:
                driver.execute_async_script("const done=arguments[0]; fetch('/api/first').then(r=>r.json()).then(done)")
                driver.switch_to.new_window('tab')
                deadline = time.monotonic() + 15
                while len(monitor._sessions) < 2 and time.monotonic() < deadline:
                    time.sleep(.1)
                self.assertGreaterEqual(len(monitor._sessions), 2)
                driver.get(url + '/second')
                driver.execute_async_script("const done=arguments[0]; fetch('/api/second').then(r=>r.json()).then(done)")
                result = driver.execute_async_script('''
                    const done = arguments[arguments.length - 1];
                    const ws = new WebSocket(arguments[0]); ws.binaryType = 'arraybuffer';
                    let count = 0;
                    ws.onopen = () => { ws.send('local-text'); ws.send(new Uint8Array([0,255,128,65])); };
                    ws.onmessage = () => { if (++count === 2) { ws.close(); done('ok'); } };
                    ws.onerror = () => done('error');
                ''', ws_url)
                self.assertEqual(result, 'ok')
                deadline = time.monotonic() + 15
                while app._websocket_frame_count < 4 and time.monotonic() < deadline:
                    time.sleep(.1)
            finally:
                monitor.pause()
                app._recording.clear()
                worker.join(timeout=30)
                monitor.stop()
            self.assertFalse(worker.is_alive())
            records = [json.loads(line) for line in app._jsonl_path.read_text().splitlines()]
            api = [r for r in records if '/api/' in r.get('url', '')]
            self.assertEqual({r['url'] for r in api}, {url+'/api/first', url+'/api/second'})
            self.assertEqual(len({r['targetId'] for r in api}), 2)
            self.assertTrue(all(json.loads(r['response_body'])['ok'] for r in api))
            frames = [r for r in records if r.get('event_type') == 'websocket_frame']
            self.assertEqual(len(frames), 4)
            binary = [r for r in frames if r['opcode'] == 2]
            self.assertEqual(len(binary), 2)
            self.assertTrue(all(base64.b64decode(r['payload_base64']) == bytes([0,255,128,65]) for r in binary))
