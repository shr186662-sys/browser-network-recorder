import base64
import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import network_capture as capture
from assets import export_assets
from privacy import redact_record, redact_url


class RecorderTests(unittest.TestCase):
    def app(self, destination, redact=False):
        app = object.__new__(capture.NetworkCaptureApp)
        app._jsonl_path = destination / 'requests.jsonl'
        app._redact = redact
        app._filter_settings = {'include_filter': 'all'}
        app._capture_count = app._websocket_frame_count = app._skipped_count = 0
        app._websocket_urls = {}
        app._lock = threading.Lock()
        app._log = lambda message: None
        app._cdp_monitor = MagicMock()
        app._cdp_monitor.get_response_body.return_value = {'body': '{"ok":true}'}
        return app

    def test_http_response_body_and_multitab_request_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.app(Path(folder))
            pending = {}
            for target in ('tab1', 'tab2'):
                app._handle_cdp_event({'method': 'Network.requestWillBeSent', '_target_id': target, 'params': {'requestId': 'same', 'request': {'url': 'https://example.test/' + target, 'method': 'GET'}, 'type': 'Fetch'}}, pending)
            for target in ('tab1', 'tab2'):
                app._handle_cdp_event({'method': 'Network.responseReceived', '_target_id': target, 'params': {'requestId': 'same', 'response': {'status': 200, 'headers': {}, 'mimeType': 'application/json'}}}, pending)
                app._handle_cdp_event({'method': 'Network.loadingFinished', '_target_id': target, 'params': {'requestId': 'same'}}, pending)
            records = [json.loads(line) for line in app._jsonl_path.read_text().splitlines()]
            self.assertEqual([r['url'] for r in records], ['https://example.test/tab1', 'https://example.test/tab2'])
            self.assertTrue(all(json.loads(r['response_body']) == {'ok': True} for r in records))

    def test_binary_websocket_is_lossless_in_raw_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.app(Path(folder))
            raw = b'\x00\xff\x80hello'
            payload = base64.b64encode(raw).decode()
            app._handle_cdp_event({'method': 'Network.webSocketCreated', '_target_id': 'one', 'params': {'requestId': 'ws', 'url': 'ws://localhost/ws'}}, {})
            app._handle_cdp_event({'method': 'Network.webSocketFrameReceived', '_target_id': 'one', 'params': {'requestId': 'ws', 'response': {'opcode': 2, 'payloadData': payload}}}, {})
            record = json.loads(app._jsonl_path.read_text())
            self.assertEqual(base64.b64decode(record['payload_base64']), raw)
            self.assertEqual(record['url'], 'ws://localhost/ws')
            self.assertEqual(app._websocket_frame_count, 1)

    def test_redaction_happens_before_disk_write(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.app(Path(folder), redact=True)
            app._write_record({'url': 'https://user:pass@example.test/api?random=top-secret#private', 'request_headers': {'Authorization': 'Bearer top-secret', 'X-Custom': 'top-secret', 'Content-Type': 'application/json'}, 'request_body': '{"nested":{"password":"top-secret"},"ok":true}', 'payload_base64': 'top-secret'})
            contents = app._jsonl_path.read_text()
            self.assertNotIn('top-secret', contents)
            self.assertNotIn('user:pass', contents)
            self.assertTrue(json.loads(contents)['redacted'])

    def test_export_does_not_escape_directory_or_use_redacted_bodies(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            records = [{'url': 'https://example.test/../../evil.js', 'resource_type': 'Script', 'response_body': 'console.log(1)'}, {'url': 'https://example.test/x.css', 'resource_type': 'Stylesheet', 'response_body': 'private', 'redacted': True}]
            (root / 'requests.jsonl').write_text('\n'.join(json.dumps(r) for r in records))
            self.assertEqual(export_assets(root), 1)
            files = list((root / 'assets').glob('*.js'))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_text(), 'console.log(1)')

    def test_raw_text_and_form_redaction(self):
        for content_type, body in [('text/plain', 'a secret'), ('application/x-www-form-urlencoded', 'custom=a-secret&password=b-secret')]:
            result = redact_record({'request_headers': {'Content-Type': content_type}, 'request_body': body})
            self.assertNotIn('secret', result['request_body'])

    def test_filter_keeps_xhr_and_excludes_script(self):
        self.assertTrue(capture.should_record_request({'resource_type': 'XHR'}, include_filter='fetch_xhr'))
        self.assertFalse(capture.should_record_request({'resource_type': 'Script'}, include_filter='fetch_xhr'))


class DriverTests(unittest.TestCase):
    def test_windows_edge_uses_matching_driver_for_detected_browser(self):
        with tempfile.TemporaryDirectory() as folder:
            edge_binary = Path(folder) / 'Microsoft' / 'Edge' / 'Application' / 'msedge.exe'
            edge_binary.parent.mkdir(parents=True)
            edge_binary.touch()
            driver_path = str(Path(folder) / 'msedgedriver.exe')
            app = object.__new__(capture.NetworkCaptureApp)
            app._log = lambda _: None
            with (
                patch.dict(capture.os.environ, {'LOCALAPPDATA': folder, 'NETWORK_RECORDER_DRIVER': 'chromedriver'}, clear=True),
                patch.object(capture, 'getproxies', return_value={'https': 'proxy.test:8080'}),
                patch('selenium.webdriver.common.selenium_manager.SeleniumManager.binary_paths',
                      return_value={'driver_path': driver_path}) as manager,
                patch('selenium.webdriver.Edge') as edge,
            ):
                edge.return_value.capabilities = {'ms:edgeOptions': {'debuggerAddress': 'localhost:9223'}}
                self.assertIs(app._create_driver('Edge'), edge.return_value)
                manager.assert_called_once_with([
                    '--browser', 'edge', '--avoid-browser-download', '--skip-driver-in-path',
                    '--proxy', 'proxy.test:8080', '--browser-path', str(edge_binary),
                ])
                self.assertEqual(edge.call_args.kwargs['options'].binary_location, str(edge_binary))
                self.assertEqual(edge.call_args.kwargs['service'].path, driver_path)
                self.assertEqual(app._debugger_address, 'localhost:9223')

    def test_edge_driver_override_works_offline(self):
        app = object.__new__(capture.NetworkCaptureApp)
        app._log = lambda _: None
        with (
            patch.dict(capture.os.environ, {'NETWORK_RECORDER_EDGE_DRIVER': 'offline/msedgedriver.exe'}, clear=True),
            patch.object(capture, 'find_windows_edge', return_value='msedge.exe'),
            patch('selenium.webdriver.common.selenium_manager.SeleniumManager.binary_paths') as manager,
            patch('selenium.webdriver.Edge') as edge,
        ):
            edge.return_value.capabilities = {'ms:edgeOptions': {'debuggerAddress': 'localhost:9223'}}
            app._create_driver('Edge')
            self.assertEqual(edge.call_args.kwargs['service'].path, 'offline/msedgedriver.exe')
            manager.assert_not_called()

    def test_missing_edge_does_not_download_a_browser(self):
        app = object.__new__(capture.NetworkCaptureApp)
        with (
            patch.dict(capture.os.environ, {}, clear=True),
            patch.object(capture.shutil, 'which', return_value=None),
            patch('selenium.webdriver.common.selenium_manager.SeleniumManager.binary_paths') as manager,
        ):
            with self.assertRaisesRegex(RuntimeError, '未找到 Microsoft Edge'):
                app._create_driver('Edge')
            manager.assert_not_called()

    def test_windows_and_mac_are_independent_of_private_downloader(self):
        for platform in ('win32', 'darwin'):
            with self.subTest(platform=platform), patch.object(capture.sys, 'platform', platform), patch.object(capture.Path, 'is_file', return_value=True), patch.object(capture, 'getproxies', return_value={}), patch('selenium.webdriver.Chrome') as chrome:
                app = object.__new__(capture.NetworkCaptureApp)
                app._log = lambda _: None
                driver = chrome.return_value
                driver.capabilities = {'goog:chromeOptions': {'debuggerAddress': 'localhost:9222'}}
                driver.execute_script.return_value = 'native user agent'
                app._create_driver()
                self.assertEqual(app._user_agent, 'native user agent')
                arguments = chrome.call_args.kwargs['options'].arguments
                self.assertNotIn('--no-sandbox', arguments)
                self.assertFalse(any(x.startswith('--user-agent=') for x in arguments))

    def test_failed_startup_closes_browser(self):
        with patch.object(capture, 'getproxies', return_value={}), patch('selenium.webdriver.Chrome') as chrome:
            chrome.return_value.execute_script.side_effect = RuntimeError('closed')
            chrome.return_value.capabilities = {'goog:chromeOptions': {'debuggerAddress': 'localhost:9222'}}
            app = object.__new__(capture.NetworkCaptureApp)
            app._log = lambda _: None
            with self.assertRaises(RuntimeError):
                app._create_driver()
            chrome.return_value.quit.assert_called_once()


if __name__ == '__main__':
    unittest.main()
