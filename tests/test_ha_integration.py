"""用假 HA 驗證：向 HA 讀資料的路徑（定位收集器、錯誤處理、不跟隨轉址）。"""
import datetime
import os
import sys
import unittest

import helpers
import fake_ha

sys.path.insert(0, os.path.join(helpers.ROOT, 'tools'))
import dash_config as C
import dash_ha
import loc_collect
from dash_support import APIError

UTC = datetime.timezone.utc


def ha_cfg(url, **extra):
    cfg = {'ha': {'url': url, 'token_env': 'DASH_FAKE_HA_TOKEN'}}
    cfg.update(extra)
    return cfg


class WithHA(unittest.TestCase):
    def setUp(self):
        os.environ['DASH_FAKE_HA_TOKEN'] = fake_ha.TOKEN

    def tearDown(self):
        os.environ.pop('DASH_FAKE_HA_TOKEN', None)

    def test_collector_against_fake_ha(self):
        now = datetime.datetime.now(UTC)
        st = {'device_tracker.p': {'state': 'not_home', 'attributes': {'latitude': 25.01, 'longitude': 121.51, 'gps_accuracy': 12},
                                   'last_changed': fake_ha.iso(now), 'last_updated': fake_ha.iso(now)}}
        ha = fake_ha.FakeHA(st, {})
        try:
            with helpers.config(ha_cfg(ha.url, commute={'entities': ['device_tracker.p', 'device_tracker.missing']})):
                db, n = loc_collect.collect(C.data_path('locations.sqlite'))
                self.assertEqual(n, 1)                         # 找不到的實體只警告、不中斷
                row = db.execute('select entity,lat,lon,gps_accuracy from locations').fetchone()
                self.assertEqual(row, ('device_tracker.p', 25.01, 121.51, 12.0))
                db.close()
                self.assertEqual(oct(os.stat(C.data_path('locations.sqlite')).st_mode & 0o777), '0o600')
        finally:
            ha.close()

    def test_errors_are_clear(self):
        ha = fake_ha.FakeHA({}, {})
        try:
            with helpers.config(ha_cfg(ha.url)):
                with self.assertRaises(APIError) as cm:
                    dash_ha.state('sensor.nope')
                self.assertEqual(cm.exception.code, 'ha_not_found')
                os.environ['DASH_FAKE_HA_TOKEN'] = 'wrong'
                with self.assertRaises(APIError) as cm:
                    dash_ha.state('sensor.x')
                self.assertEqual(cm.exception.code, 'ha_auth')
        finally:
            ha.close()
        with helpers.config(ha_cfg('http://127.0.0.1:1')):
            with self.assertRaises(APIError) as cm:
                dash_ha.state('sensor.x')
            self.assertEqual(cm.exception.code, 'ha_unreachable')


class Redirects(unittest.TestCase):
    """帶權杖的請求絕不跟隨重新導向（避免權杖被送到別的來源）。"""

    def test_redirect_is_refused_and_token_not_forwarded(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        seen = []

        class Target(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                seen.append(self.headers.get('Authorization'))
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{}')

        target = ThreadingHTTPServer(('127.0.0.1', 0), Target)

        class Redirector(BaseHTTPRequestHandler):
            code = 302

            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(self.code)
                self.send_header('Location', f'http://127.0.0.1:{target.server_port}/other-origin')
                self.end_headers()

        origin = ThreadingHTTPServer(('127.0.0.1', 0), Redirector)
        for srv in (target, origin):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        os.environ['DASH_FAKE_HA_TOKEN'] = 'synthetic-secret-token'
        try:
            with helpers.config(ha_cfg(f'http://127.0.0.1:{origin.server_port}')):
                for code in (301, 302, 303, 307, 308):
                    Redirector.code = code
                    with self.subTest(code=code):
                        with self.assertRaises(APIError) as cm:
                            dash_ha.get('/api/states')
                        self.assertEqual(cm.exception.code, 'ha_redirect')
                        self.assertIn('ha.url', str(cm.exception))
        finally:
            os.environ.pop('DASH_FAKE_HA_TOKEN', None)
            for srv in (target, origin):
                srv.shutdown()
                srv.server_close()
        self.assertEqual(seen, [])           # 另一個來源完全沒收到請求，更不可能收到權杖


if __name__ == '__main__':
    unittest.main()
