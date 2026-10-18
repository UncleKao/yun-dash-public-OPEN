"""端到端：真的啟動 HTTP 伺服器（臨時埠、臨時資料夾），逐一打每個端點。"""
import http.client
import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
import unittest
import urllib.parse

import helpers

sys.path.insert(0, os.path.join(helpers.ROOT, 'tools'))
import dash_config as C
import dash_food as F
import food_import
import server

server.LOG_REQUESTS = False     # 測試時不要刷 stderr
server.LOGIN_BACKOFF_BASE = 0   # 預設測試不鎖定（連續失敗的測試才會用到真的鎖定，見 LoginThrottle）

GET_PATHS = ['/', '/app.js', '/app.css', '/favicon.svg', '/favicon.ico', '/api/features', '/api/mapkey', '/api/track',
             '/api/track?date=2030-01-07', '/api/food/stats', '/api/food/dice', '/api/food/near', '/api/food/districts',
             '/api/food/cats', '/api/food/list', '/api/food/ate', '/api/custom', '/api/metro/arrivals', '/api/metro/crowding']
CARD = {'type': 'value', 'title': '測試', 'entities': ['sensor.x']}


class Running:
    """with Running(config) as call: call('GET', '/api/features') → (status, headers, body)"""

    def __init__(self, overrides=None):
        self.cm = helpers.config(overrides)

    def __enter__(self):
        self.dir = self.cm.__enter__()
        self.httpd = server.make_server('127.0.0.1', 0)
        self.port = self.httpd.server_address[1]
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()
        return self.call

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.cm.__exit__(*a)

    def call(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        h = dict(headers or {})
        data = None
        if body is not None:
            data = body if isinstance(body, (bytes, str)) else json.dumps(body)
            h.setdefault('Content-Type', 'application/json')
        c.request(method, urllib.parse.quote(path, safe='/?&=:%'), data, h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, dict(r.getheaders()), raw


def js(raw):
    return json.loads(raw.decode())


class BlankSetup(unittest.TestCase):
    """完全沒設定：每個端點都不能 500，沒設定的回 configured:false。"""

    def test_every_get_endpoint(self):
        with Running() as call:
            for p in GET_PATHS:
                status, _, raw = call('GET', p)
                with self.subTest(path=p):
                    self.assertIn(status, (200, 204, 404), p)
                    self.assertNotEqual(status, 500)
                    if p.startswith('/api/') and status == 200:
                        d = js(raw)
                        self.assertTrue(d.get('configured') is False or 'reason' not in d)
            self.assertEqual(call('GET', '/api/mapkey')[0], 404)
            st, _, raw = call('GET', '/api/features')
            f = js(raw)['features']
            self.assertFalse(f['commute']['ready'])
            self.assertFalse(f['custom']['ready'])
            self.assertIn('custom.cards', f['custom']['reason'])
            self.assertEqual(call('GET', '/nope')[0], 404)

    def test_post_validation_not_500(self):
        with Running() as call:
            self.assertEqual(call('POST', '/api/food/tried', {'id': 'abc'})[0], 400)
            self.assertEqual(call('POST', '/api/food/tried', {'id': 1})[0], 200)       # 沒有美食庫 → configured:false
            self.assertEqual(call('POST', '/api/food/tried', b'[1,2]')[0], 400)
            self.assertEqual(call('POST', '/api/food/tried', b'{broken')[0], 400)
            self.assertEqual(call('POST', '/api/nope', {})[0], 404)

    def test_oversized_body_rejected(self):
        with Running() as call:
            big = json.dumps({'id': 1, 'note': 'x' * (300 * 1024)})
            self.assertEqual(call('POST', '/api/food/tried', big)[0], 400)


class FullSetup(unittest.TestCase):
    def test_food_through_http(self):
        with Running({'places': {'車站': {'lat': 25.0478, 'lon': 121.5170}}}) as call:
            C.ensure_data_dir()
            db = F.init_db()
            food_import.import_csv(os.path.join(helpers.ROOT, 'data.example', 'places.sample.csv'), db)
            db.close()
            feats = js(call('GET', '/api/features')[2])
            self.assertTrue(feats['features']['food']['ready'])
            self.assertEqual(js(call('GET', '/api/food/stats')[2])['places'], 12)
            d = js(call('GET', '/api/food/dice?place=all&open=0')[2])
            self.assertIsNotNone(d['pick'])
            self.assertEqual(js(call('GET', '/api/food/near?near=車站&radius=300')[2])['radius'], 300)
            self.assertEqual(js(call('GET', '/api/food/list?near=all&q=咖啡')[2])['total'], 1)
            self.assertEqual(call('GET', '/api/food/cats?near=不存在')[0], 400)
            pid = d['pick']['id']
            self.assertEqual(call('POST', '/api/food/tried', {'id': pid})[0], 200)
            self.assertEqual(js(call('GET', '/api/food/ate')[2])['total'], 1)
            self.assertEqual(call('POST', '/api/food/tried', {'id': 999999})[0], 404)

    def test_value_error_from_a_handler_is_422_not_500(self):
        with Running() as call:
            orig = server.GET_ROUTES['/api/food/stats']
            server.GET_ROUTES['/api/food/stats'] = ('food', lambda q: (_ for _ in ()).throw(ValueError('資料檔 x.json 無法讀取')))
            try:
                st, _, raw = call('GET', '/api/food/stats')
            finally:
                server.GET_ROUTES['/api/food/stats'] = orig
            self.assertEqual(st, 422)
            self.assertIn('x.json', js(raw)['error'])

    def test_disabled_feature_is_404(self):
        with Running({'features': {'food': False}}) as call:
            st, _, raw = call('GET', '/api/food/stats')
            self.assertEqual(st, 404)
            self.assertTrue(js(raw)['disabled'])
            self.assertFalse(js(call('GET', '/api/features')[2])['features']['food']['enabled'])

    def test_ha_unreachable_is_502_not_crash(self):
        with Running({'ha': {'url': 'http://127.0.0.1:1', 'token_env': 'DASH_TEST_HA'}, 'custom': {'cards': [CARD]}}) as call:
            os.environ['DASH_TEST_HA'] = 'tok'
            try:
                for p in ('/api/custom',):
                    st, _, raw = call('GET', p)
                    self.assertEqual(st, 502, p)
                    self.assertIn('Home Assistant', js(raw)['error'])
            finally:
                del os.environ['DASH_TEST_HA']


TOK = 'tok-abcdefghij123'


class Login(unittest.TestCase):
    def test_login_page_flow_without_secret_in_url(self):
        with Running({'server': {'access_token': TOK}}) as call:
            st, h, _ = call('GET', '/')
            self.assertEqual((st, h['Location']), (302, '/login'))
            st, h, raw = call('GET', '/login')
            self.assertEqual(st, 200)
            self.assertIn('type="password"', raw.decode())
            self.assertEqual(call('GET', '/api/features')[0], 401)
            self.assertEqual(call('GET', '/app.js')[0], 302)                 # 靜態檔也要先登入
            form = {'Content-Type': 'application/x-www-form-urlencoded'}
            st, _, raw = call('POST', '/login', 'token=wrong-wrong-wrong', form)
            self.assertEqual(st, 401)
            self.assertIn('密語不正確', raw.decode())
            st, h, _ = call('POST', '/login', 'token=' + TOK, form)
            self.assertEqual((st, h['Location']), (303, '/'))
            cookie = h['Set-Cookie']
            self.assertIn('HttpOnly', cookie)
            self.assertIn('SameSite=Strict', cookie)
            self.assertNotIn('Secure', cookie)                                # 純 HTTP 不加 Secure
            self.assertNotIn(TOK, cookie)                                     # cookie 不含密語
            c = {'Cookie': cookie.split(';')[0]}
            self.assertEqual(call('GET', '/api/features', headers=c)[0], 200)
            self.assertEqual(call('GET', '/', headers=c)[0], 200)
            self.assertEqual(call('GET', '/login', headers=c)[0], 302)       # 已登入就回首頁
            self.assertEqual(call('GET', '/api/features', headers={'X-Dash-Token': TOK})[0], 200)
            # 程式用 JSON 登入
            self.assertEqual(call('POST', '/login', {'token': TOK})[0], 303)
            self.assertEqual(call('POST', '/login', {'token': 'x' * 14})[0], 401)

    def test_query_string_token_no_longer_logs_in(self):
        with Running({'server': {'access_token': TOK}}) as call:
            st, h, _ = call('GET', '/?token=' + TOK)
            self.assertEqual((st, h['Location']), (302, '/login'))
            self.assertNotIn('Set-Cookie', h)

    def test_login_is_origin_checked_and_post_requires_login(self):
        with Running({'server': {'access_token': TOK}}) as call:
            form = {'Content-Type': 'application/x-www-form-urlencoded', 'Origin': 'http://evil.example'}
            self.assertEqual(call('POST', '/login', 'token=' + TOK, form)[0], 403)
            self.assertEqual(call('POST', '/api/food/tried', {'id': 1})[0], 401)

    def test_login_without_token_configured_redirects_home(self):
        with Running() as call:
            self.assertEqual(call('GET', '/login')[0], 302)
            self.assertEqual(call('POST', '/login', {'token': 'whatever-token'})[0], 302)

    def test_oversized_login_body_rejected(self):
        with Running({'server': {'access_token': TOK}}) as call:
            self.assertEqual(call('POST', '/login', 'token=' + 'a' * 10000, {'Content-Type': 'application/x-www-form-urlencoded'})[0], 400)


class HostAndOrigin(unittest.TestCase):
    def test_unknown_host_is_rejected_even_from_loopback(self):
        with Running() as call:
            evil = {'Host': 'attacker.example:1234'}
            self.assertEqual(call('GET', '/api/features', headers=evil)[0], 403)
            self.assertEqual(call('GET', '/api/food/stats', headers=evil)[0], 403)
            # 攻擊者網頁的 POST：Host 與 Origin 一致也要被擋
            self.assertEqual(call('POST', '/api/food/tried', {'id': 1}, dict(evil, Origin='http://attacker.example:1234'))[0], 403)
            self.assertEqual(call('GET', '/api/features', headers={'Host': 'localhost:1'})[0], 200)

    def test_allowed_hosts(self):
        with Running({'server': {'allowed_hosts': ['home-dash.lan']}}) as call:
            self.assertEqual(call('GET', '/api/features', headers={'Host': 'home-dash.lan:9877'})[0], 200)
            self.assertEqual(call('GET', '/api/features', headers={'Host': 'other.lan'})[0], 403)

    def test_origin_must_match_scheme_host_port(self):
        with Running() as call:
            port = call.__self__.port
            ok = {'Origin': f'http://127.0.0.1:{port}'}
            self.assertEqual(call('POST', '/api/food/tried', {'id': 1}, ok)[0], 200)
            for bad in (f'https://127.0.0.1:{port}', f'http://127.0.0.1:{port + 1}', 'http://evil.example', 'null'):
                with self.subTest(origin=bad):
                    self.assertEqual(call('POST', '/api/food/tried', {'id': 1}, {'Origin': bad})[0], 403)
            self.assertEqual(call('POST', '/api/food/tried', {'id': 1}, {'Sec-Fetch-Site': 'cross-site'})[0], 403)

    def test_other_network_forbidden(self):
        with Running({'server': {'allowed_networks': ['10.99.0.0/16']}}) as call:
            st, _, raw = call('GET', '/api/features')
            self.assertEqual(st, 403)
            self.assertIn('allowed_networks', js(raw)['error'])


class StartupChecks(unittest.TestCase):
    def test_non_local_bind_without_token_refuses(self):
        with helpers.config():
            with self.assertRaises(C.ConfigError) as cm:
                server.make_server('0.0.0.0', 0)
            self.assertIn('access_token', str(cm.exception))
            with self.assertRaises(SystemExit) as cm2:
                import tempfile
                with tempfile.TemporaryDirectory() as d:
                    p = os.path.join(d, 'c.json')
                    with open(p, 'w') as f:
                        json.dump({'data_dir': d}, f)
                    server.main(['--config', p, '--host', '0.0.0.0', '--port', '0'])
            self.assertIn('access_token', str(cm2.exception))

    def test_non_local_bind_with_token_starts(self):
        with helpers.config({'server': {'access_token': TOK}}):
            httpd = server.make_server('0.0.0.0', 0)
            httpd.server_close()

    def test_loopback_without_token_is_fine(self):
        with helpers.config():
            for h in ('127.0.0.1', 'localhost'):
                server.make_server(h, 0).server_close()


@unittest.skipUnless(shutil.which('openssl'), '需要 openssl 產生測試憑證')
class Tls(unittest.TestCase):
    def test_builtin_https_sets_secure_cookie(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cert, key = os.path.join(d, 'c.pem'), os.path.join(d, 'k.pem')
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', key, '-out', cert, '-days', '1',
                            '-subj', '/CN=localhost'], check=True, capture_output=True)
            with helpers.config({'server': {'access_token': TOK, 'tls_cert': cert, 'tls_key': key}}):
                httpd = server.make_server('127.0.0.1', 0)
                threading.Thread(target=httpd.serve_forever, daemon=True).start()
                try:
                    ctx = ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                    port = httpd.server_address[1]
                    c = http.client.HTTPSConnection('127.0.0.1', port, context=ctx, timeout=10)
                    c.request('POST', '/login', 'token=' + TOK, {'Content-Type': 'application/x-www-form-urlencoded',
                                                                'Origin': f'https://127.0.0.1:{port}'})
                    r = c.getresponse()
                    r.read()
                    self.assertEqual(r.status, 303)
                    self.assertIn('Secure', r.getheader('Set-Cookie'))
                finally:
                    httpd.shutdown()
                    httpd.server_close()

    def test_bad_cert_is_a_clear_config_error(self):
        with helpers.config({'server': {'tls_cert': '/nonexistent.pem', 'tls_key': '/nonexistent.key'}}):
            with self.assertRaises(C.ConfigError):
                server.make_server('127.0.0.1', 0)


class Headers(unittest.TestCase):
    def test_security_headers(self):
        with Running() as call:
            for path in ('/', '/api/features', '/nope'):
                _, h, _ = call('GET', path)
                self.assertEqual(h['X-Frame-Options'], 'DENY')
                self.assertEqual(h['Referrer-Policy'], 'same-origin')   # 不能是 no-referrer：會讓瀏覽器表單 POST 送 Origin: null
                self.assertEqual(h['X-Content-Type-Options'], 'nosniff')
                self.assertIn("frame-ancestors 'none'", h['Content-Security-Policy'])
            csp = call('GET', '/')[1]['Content-Security-Policy']
            self.assertIn("script-src 'self' https://unpkg.com", csp)
            self.assertIn("default-src 'none'", csp)

    def test_google_key_switches_to_report_only(self):
        with Running({'map': {'google_maps_js_key': 'k'}}) as call:
            h = call('GET', '/')[1]
            self.assertNotIn("default-src", h['Content-Security-Policy'])
            self.assertIn('googleapis.com', h['Content-Security-Policy-Report-Only'])

    def test_index_uses_sri_for_cdn_assets(self):
        with Running() as call:
            html = call('GET', '/')[2].decode()
            for tag in ('<link rel="stylesheet" href="https://unpkg.com', '<script src="https://unpkg.com'):
                line = [t for t in html.split('\n') if tag in t][0]
                self.assertIn('integrity="sha384-', line)
                self.assertIn('crossorigin="anonymous"', line)
            js_src = call('GET', '/app.js')[2].decode()
            self.assertIn("googlemutant", js_src.lower())
            self.assertIn("sha384-", js_src)


class RequestLog(unittest.TestCase):
    def test_logs_method_and_path_without_query(self):
        import contextlib
        import io
        server.LOG_REQUESTS = True
        buf = io.StringIO()
        try:
            with Running() as call, contextlib.redirect_stderr(buf):
                call('GET', '/api/food/stats?secret=hunter2')
                call('GET', '/api/features', headers={'Host': 'evil.example'})
        finally:
            server.LOG_REQUESTS = False
        log = buf.getvalue()
        self.assertIn('GET /api/food/stats 200', log)
        self.assertIn('GET /api/features 403', log)          # 被拒絕的請求也查得到
        self.assertNotIn('hunter2', log)
        self.assertNotIn('secret', log)


class FeatureStatusWithBadTokenFile(unittest.TestCase):
    def test_loose_token_file_shows_reason_not_500(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, 'tok')
            with open(f, 'w') as fh:
                fh.write('t')
            os.chmod(f, 0o644)
            with Running({'ha': {'url': 'http://ha.invalid', 'token_env': 'DASH_NO_SUCH_ENV', 'token_file': f},
                          'custom': {'cards': [CARD]}}) as call:
                st, _, raw = call('GET', '/api/features')
                self.assertEqual(st, 200)
                feats = js(raw)['features']
                self.assertIn('chmod 600', feats['custom']['reason'])
                for p in ('/api/custom',):
                    st, _, raw = call('GET', p)
                    self.assertEqual(st, 200, p)
                    self.assertFalse(js(raw)['configured'])
                    self.assertIn('chmod 600', js(raw)['reason'])


class StaticAndSecrets(unittest.TestCase):
    def test_static_paths_cannot_escape(self):
        with Running() as call:
            for p in ('/../server.py', '/static/../server.py', '/%2e%2e/server.py', '/config.json', '/data/food.sqlite'):
                self.assertEqual(call('GET', p)[0], 404, p)

    def test_no_secret_in_features(self):
        with Running({'map': {'google_maps_js_key': 'KEY-VISIBLE-ON-PURPOSE'}, 'ha': {'url': 'http://h.invalid'},
                      'server': {'access_token': TOK}}) as call:
            raw = call('GET', '/api/features', headers={'X-Dash-Token': TOK})[2].decode()
            for secret in ('KEY-VISIBLE-ON-PURPOSE', TOK):
                self.assertNotIn(secret, raw)


class MoreValidationThroughHttp(unittest.TestCase):
    def test_nonfinite_numbers_elsewhere_are_400(self):
        with Running() as call:
            for raw in ('{"id": Infinity}', '{"id": NaN}', '{"id": -Infinity}', '{"id": 1, "rating": NaN}', '{"id": "inf"}', '{"id": 1e999}'):
                self.assertEqual(call('POST', '/api/food/tried', raw)[0], 400, raw)

    def test_double_minus_integer_is_400(self):
        with Running() as call:
            self.assertEqual(call('GET', '/api/track?date=--5')[0], 400)
            self.assertEqual(call('POST', '/api/food/tried', {'id': '--5'})[0], 400)


if __name__ == '__main__':
    unittest.main()
