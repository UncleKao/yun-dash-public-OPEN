"""第二輪覆核的回歸測試：設定檔壞掉、輸入驗證、日誌跳脫、逾時、登入鎖定、cookie 期限、HA 回應防禦、快取、資料夾權限。"""
import contextlib
import datetime
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import helpers
import fake_ha
import dash_access
import dash_config as C
import dash_ha
import server
from dash_support import APIError, NotConfigured, TimedCache
from test_server import Running, js

ROOT = helpers.ROOT
TOK = 'tok-abcdefghij123'


def run_py(args, env_extra=None, timeout=20):
    env = dict(os.environ)
    env.pop('HA_TOKEN', None)
    env.update(env_extra or {})
    return subprocess.run([sys.executable] + args, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)


class BrokenConfigIsFriendly(unittest.TestCase):
    """設定檔壞掉：stderr 要是中文訊息，不能有 Traceback。"""
    BAD = {'多一個逗號': '{"title": "x",}', '少引號': '{"title: "x"}', '最外層是清單': '[1, 2]',
           '埠不是數字': '{"server": {"port": "abc"}}', '空檔': '', '價位分層反了': '{"food": {"price_tiers": [900, 100]}}'}

    def with_bad(self, text):
        d = tempfile.mkdtemp()
        p = os.path.join(d, 'config.json')
        with open(p, 'w', encoding='utf-8') as f:
            f.write(text)
        return p

    def check(self, r, label):
        self.assertNotEqual(r.returncode, 0, label)
        self.assertIn('設定檔有問題', r.stderr, label)
        self.assertNotIn('Traceback', r.stderr, label)

    def test_server_and_tools_exit_with_chinese_message(self):
        for label, text in self.BAD.items():
            p = self.with_bad(text)
            env = {'HOME_DASH_CONFIG': p}
            with self.subTest(label=label):
                self.check(run_py(['server.py', '--port', '0'], env), f'server {label}')
                self.check(run_py(['tools/loc_collect.py'], env), f'loc_collect {label}')
                self.check(run_py(['tools/food_import.py', '--init'], env), f'food_import {label}')

    def test_import_never_raises(self):
        r = run_py(['-c', 'import dash_config, dash_food, dash_custom, dash_metro, server; print("ok", bool(dash_config.CONFIG_ERROR))'],
                   {'HOME_DASH_CONFIG': self.with_bad('{"a":,}')})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('ok True', r.stdout)

    def test_explicit_good_config_wins_over_broken_default(self):
        d = tempfile.mkdtemp()
        good = os.path.join(d, 'good.json')
        with open(good, 'w') as f:
            json.dump({'data_dir': d}, f)
        p = subprocess.Popen([sys.executable, 'server.py', '--config', good, '--port', '0'], cwd=ROOT, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=dict(os.environ, HOME_DASH_CONFIG=self.with_bad('{,}')))
        try:
            line = p.stdout.readline()
            self.assertIn('啟動', line)
        finally:
            p.terminate()
            err = p.stderr.read()
            p.wait(timeout=10)
        self.assertNotIn('Traceback', err)

    def test_reload_failure_keeps_old_config(self):
        with helpers.config({'title': '舊標題'}) as d:
            bad = os.path.join(d, 'bad.json')
            with open(bad, 'w') as f:
                f.write('{,}')
            with self.assertRaises(C.ConfigError):
                C.reload(bad)
            self.assertEqual(C.CFG['title'], '舊標題')


class PlacesValidation(unittest.TestCase):
    def load(self, places):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'c.json')
            with open(p, 'w', encoding='utf-8') as f:
                json.dump({'places': places}, f)
            return C.load(p)

    def test_bad_radius_icon_and_coordinates_fail_at_startup(self):
        for bad in ({'radius': 'abc'}, {'radius': 0}, {'radius': -5}, {'radius': 1e9}, {'radius': True}, {'radius': None},
                    {'icon': 7}, {'icon': 'x' * 17}, {'icon': None}):
            with self.subTest(bad=bad), self.assertRaises(C.ConfigError):
                self.load({'家': dict({'lat': 25.0, 'lon': 121.5}, **bad)})
        for lat, lon in ((91, 0), (-91, 0), (0, 181), (0, -181)):
            with self.subTest(lat=lat, lon=lon), self.assertRaises(C.ConfigError):
                self.load({'x': {'lat': lat, 'lon': lon}})

    def test_good_values(self):
        cfg = self.load({'家': {'lat': 25.0, 'lon': 121.5, 'radius': 70.5, 'icon': '🏠'}, '點': {'lat': -33.9, 'lon': 151.2}})
        self.assertEqual(cfg['places']['家']['radius'], 70.5)


class FoodParamValidation(unittest.TestCase):
    def test_non_integer_radius_and_price_are_400_in_chinese(self):
        with Running() as call:
            for path in ('/api/food/list?near=all&radius=%C2%B2', '/api/food/list?near=all&radius=abc', '/api/food/list?near=all&radius=0',
                         '/api/food/list?near=all&radius=-5', '/api/food/list?near=all&radius=100001', '/api/food/dice?place=all&price=%C2%B2',
                         '/api/food/dice?place=all&price=-1', '/api/food/dice?place=all&price=1e3', '/api/food/cats?near=all&radius=1.5',
                         '/api/food/near?near=gps&radius=abc'):
                with self.subTest(path=path):
                    # 沒有美食庫時先回 configured:false；有庫才會驗參數，所以先建庫
                    pass
            import dash_food as F
            import food_import
            C.ensure_data_dir()
            db = F.init_db()
            food_import.import_csv(os.path.join(ROOT, 'data.example', 'places.sample.csv'), db)
            db.close()
            for path in ('/api/food/list?near=all&radius=%C2%B2', '/api/food/list?near=all&radius=abc', '/api/food/list?near=all&radius=0',
                         '/api/food/list?near=all&radius=-5', '/api/food/list?near=all&radius=100001', '/api/food/dice?place=all&price=%C2%B2',
                         '/api/food/dice?place=all&price=-1', '/api/food/dice?place=all&price=1e3', '/api/food/cats?near=all&radius=1.5'):
                with self.subTest(path=path):
                    st, _, raw = call('GET', path)
                    self.assertEqual(st, 400, path)
                    err = js(raw)['error']
                    self.assertNotIn('invalid literal', err)
                    self.assertTrue(any('一' <= ch <= '鿿' for ch in err), err)
            self.assertEqual(call('GET', '/api/food/list?near=all&radius=800')[0], 200)
            self.assertEqual(call('GET', '/api/food/list?near=all&radius=')[0], 200)         # 空白＝預設
            self.assertEqual(call('GET', '/api/food/dice?place=all&price=400&open=0')[0], 200)


class CaFileAndStatic(unittest.TestCase):
    def test_missing_ca_file_is_explained_not_404(self):
        os.environ['DASH_FAKE_HA_TOKEN'] = 't'
        try:
            with Running({'ha': {'url': 'https://ha.invalid', 'token_env': 'DASH_FAKE_HA_TOKEN', 'ca_file': '/no/such/ca.pem'},
                          'custom': {'cards': [{'type': 'value', 'title': 't', 'entities': ['sensor.x']}]}}) as call:
                st, _, raw = call('GET', '/api/custom')
                self.assertEqual(st, 200)
                d = js(raw)
                self.assertFalse(d['configured'])
                self.assertIn('ha.ca_file', d['reason'])
                self.assertIn('/no/such/ca.pem', d['reason'])
        finally:
            del os.environ['DASH_FAKE_HA_TOKEN']

    def test_filenotfound_elsewhere_is_not_masked_as_404(self):
        with Running() as call:
            orig = server.api_features
            server.api_features = lambda q: (_ for _ in ()).throw(FileNotFoundError('設定相關檔案不見了'))
            try:
                st, _, raw = call('GET', '/api/features')
            finally:
                server.api_features = orig
            self.assertEqual(st, 500)
            self.assertIn('FileNotFoundError', js(raw)['error'])

    def test_missing_static_file_is_404(self):
        with Running() as call:
            orig = server.STATIC
            server.STATIC = '/nonexistent-dir'
            try:
                self.assertEqual(call('GET', '/app.js')[0], 404)
            finally:
                server.STATIC = orig


class RequestLogEscaping(unittest.TestCase):
    def test_control_characters_are_escaped(self):
        server.LOG_REQUESTS = True
        buf = io.StringIO()
        try:
            with Running() as call, contextlib.redirect_stderr(buf):
                port = call.__self__.port
                s = socket.create_connection(('127.0.0.1', port), timeout=5)
                s.sendall(b'GET /\x1b[31mRED\x07\x08BYE HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n')
                s.recv(4096)
                s.close()
                time.sleep(0.2)
        finally:
            server.LOG_REQUESTS = False
        log = buf.getvalue()
        for ch in ('\x1b', '\x07', '\x08'):
            self.assertNotIn(ch, log)
        self.assertIn('\\x1b[31mRED\\x07\\x08BYE', log)
        self.assertEqual(log.count('\n'), 1)


class Timeouts(unittest.TestCase):
    def test_default_is_30_seconds(self):
        self.assertEqual(server.Handler.timeout, 30)

    def test_half_open_request_is_dropped(self):
        orig = server.Handler.timeout
        server.Handler.timeout = 1
        try:
            with Running() as call:
                s = socket.create_connection(('127.0.0.1', call.__self__.port), timeout=10)
                s.sendall(b'POST /api/food/tried HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 50\r\n\r\n')   # 宣告有本文卻不送
                t = time.time()
                data = s.recv(4096)               # 逾時後伺服器關閉連線（可能先回錯誤或直接 EOF），不會永遠掛著
                self.assertLess(time.time() - t, 6)
                s.close()
                self.assertEqual(call('GET', '/api/features')[0], 200)      # 其他請求照常
        finally:
            server.Handler.timeout = orig


class LoginThrottle(unittest.TestCase):
    def setUp(self):
        server._login_fails.clear()
        self.orig = server.LOGIN_BACKOFF_BASE
        server.LOGIN_BACKOFF_BASE = 1.0

    def tearDown(self):
        server.LOGIN_BACKOFF_BASE = self.orig
        server._login_fails.clear()

    def test_progressive_lockout_per_ip(self):
        bad = lambda: False
        self.assertEqual(server.login_gate('1.1.1.1', bad, now=100)[0], 'bad')              # 第 1 次失敗：鎖 1 秒
        st, wait = server.login_gate('1.1.1.1', bad, now=100.5)
        self.assertEqual(st, 'locked')
        self.assertAlmostEqual(wait, 0.5)
        self.assertEqual(server.login_gate('1.1.1.1', bad, now=101.1)[0], 'bad')            # 第 2 次失敗：鎖 2 秒
        self.assertEqual(server.login_gate('1.1.1.1', bad, now=102)[0], 'locked')
        self.assertEqual(server.login_gate('1.1.1.1', bad, now=103.2)[0], 'bad')            # 第 3 次：鎖 4 秒
        self.assertEqual(server.login_gate('1.1.1.1', bad, now=106)[0], 'locked')
        self.assertEqual(server.login_gate('2.2.2.2', bad, now=106)[0], 'bad')              # 別的來源不受影響

    def test_locked_state_does_not_even_check_the_password(self):
        server.login_gate('1.1.1.1', lambda: False, now=100)
        calls = []
        st, _ = server.login_gate('1.1.1.1', lambda: calls.append(1) or True, now=100.2)
        self.assertEqual(st, 'locked')
        self.assertEqual(calls, [])                      # 鎖定期間連正確密語都不採信

    def test_success_clears_record_and_cap(self):
        server.login_gate('1.1.1.1', lambda: False, now=100)
        self.assertEqual(server.login_gate('1.1.1.1', lambda: True, now=102)[0], 'ok')
        self.assertNotIn('1.1.1.1', server._login_fails)
        for i in range(8):
            server.login_gate('3.3.3.3', lambda: False, now=1000 + i * 100)
        self.assertLessEqual(server._login_fails['3.3.3.3'][1] - (1000 + 7 * 100), server.LOGIN_BACKOFF_MAX)

    def test_stale_records_are_dropped(self):
        server.login_gate('1.1.1.1', lambda: False, now=100)
        server.login_gate('9.9.9.9', lambda: True, now=1000)
        self.assertNotIn('1.1.1.1', server._login_fails)

    def test_parallel_guessing_does_not_speed_up(self):
        results, lock = [], threading.Lock()

        def attempt():
            r = server.login_gate('7.7.7.7', lambda: False)[0]
            with lock:
                results.append(r)
        ts = [threading.Thread(target=attempt) for _ in range(30)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(results.count('bad'), 1)          # 30 個並行猜測，只有 1 個真的被檢查
        self.assertEqual(results.count('locked'), 29)

    def test_http_flow(self):
        with Running({'server': {'access_token': TOK}}) as call:
            server.LOGIN_BACKOFF_BASE = 0.4
            form = {'Content-Type': 'application/x-www-form-urlencoded'}
            self.assertEqual(call('POST', '/login', 'token=wrong-wrong-wrong', form)[0], 401)
            st, h, raw = call('POST', '/login', 'token=' + TOK, form)           # 鎖定中，連正確的也不收
            self.assertEqual(st, 429)
            self.assertIn('Retry-After', h)
            self.assertIn('失敗次數太多', raw.decode())
            st, h, _ = call('POST', '/login', {'token': TOK})
            self.assertEqual(st, 429)
            time.sleep(0.5)
            st, h, _ = call('POST', '/login', 'token=' + TOK, form)
            self.assertEqual(st, 303)
            self.assertIn('Set-Cookie', h)


class SessionCookie(unittest.TestCase):
    def setUp(self):
        self.a = dash_access.Access({'allowed_networks': ['127.0.0.1/32'], 'access_token': TOK})

    def test_format_and_validity_window(self):
        now = 1_800_000_000
        v = self.a.new_session(now)
        issued, sig = v.split('.')
        self.assertEqual(issued, str(now))
        self.assertTrue(self.a.session_ok(v, now + 5))
        self.assertTrue(self.a.session_ok(v, now + dash_access.SESSION_SECONDS))
        self.assertFalse(self.a.session_ok(v, now + dash_access.SESSION_SECONDS + 1))      # 伺服器端 30 天到期
        self.assertFalse(self.a.session_ok(v, now - 3600))                                  # 簽發時間在未來
        self.assertTrue(self.a.session_ok(v, now - 30))                                     # 容許些微時鐘誤差

    def test_tampering_rejected(self):
        now = 1_800_000_000
        v = self.a.new_session(now)
        _, sig = v.split('.')
        for bad in (f'{now + 1}.{sig}', f'{now}.{sig[:-1]}0', f'{now}.', f'.{sig}', sig, '', f'x.{sig}', f'{now}.{sig}.{sig}',
                    f'{"9" * 20}.{sig}', f'١٢٣.{sig}'):
            with self.subTest(bad=bad):
                self.assertFalse(self.a.session_ok(bad, now))

    def test_expired_cookie_rejected_through_headers(self):
        import email
        old = self.a.new_session(time.time() - dash_access.SESSION_SECONDS - 100)
        h = email.message_from_string(f'Cookie: dash_token={old}\n\n')
        self.assertFalse(self.a.token_ok(h))
        fresh = email.message_from_string(f'Cookie: {self.a.cookie_header().split(";")[0]}\n\n')
        self.assertTrue(self.a.token_ok(fresh))

    def test_changing_token_revokes_old_cookies(self):
        import email
        c = self.a.cookie_header().split(';')[0]
        other = dash_access.Access({'allowed_networks': ['127.0.0.1/32'], 'access_token': TOK + 'z'})
        self.assertFalse(other.token_ok(email.message_from_string(f'Cookie: {c}\n\n')))

    def test_max_age_matches_server_side_window(self):
        self.assertIn(f'Max-Age={dash_access.SESSION_SECONDS}', self.a.cookie_header())


class TimedCacheConcurrency(unittest.TestCase):
    def test_slow_build_does_not_block_other_keys(self):
        c = TimedCache()
        release = threading.Event()
        started = threading.Event()

        def slow():
            started.set()
            release.wait(10)
            return 'slow'
        t = threading.Thread(target=lambda: c.get('a', 60, slow))
        t.start()
        self.assertTrue(started.wait(5))
        t0 = time.time()
        self.assertEqual(c.get('b', 60, lambda: 'fast'), 'fast')          # 另一個 key 立刻完成
        self.assertLess(time.time() - t0, 1)
        release.set()
        t.join(5)
        self.assertEqual(c.get('a', 60, lambda: 'never'), 'slow')

    def test_same_key_is_built_once_for_concurrent_callers(self):
        c = TimedCache()
        calls, out = [], []

        def build():
            calls.append(1)
            time.sleep(0.3)
            return 'v'
        ts = [threading.Thread(target=lambda: out.append(c.get('k', 60, build))) for _ in range(8)]
        [t.start() for t in ts]
        [t.join(5) for t in ts]
        self.assertEqual(len(calls), 1)
        self.assertEqual(out, ['v'] * 8)

    def test_exceptions_not_cached_and_limit_and_ttl(self):
        c = TimedCache(limit=2)
        with self.assertRaises(RuntimeError):
            c.get('k', 60, lambda: (_ for _ in ()).throw(RuntimeError('x')))
        self.assertEqual(c.get('k', 60, lambda: 'ok'), 'ok')                # 失敗沒有被記住
        c.get('a', 60, lambda: 1)
        c.get('b', 60, lambda: 2)
        self.assertEqual(len(c.data), 2)
        self.assertEqual(c._building, {})                                     # 建置鎖不會洩漏
        now = [0.0]
        c2 = TimedCache(clock=lambda: now[0])
        c2.get('k', 10, lambda: 'old')
        now[0] = 11
        self.assertEqual(c2.get('k', 10, lambda: 'new'), 'new')              # 過期重建


class DataDirPermissions(unittest.TestCase):
    def test_warns_when_directory_is_too_open(self):
        with helpers.config() as d:
            data = os.path.join(d, 'data')
            os.makedirs(data, mode=0o755)
            os.chmod(data, 0o755)
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                C.ensure_data_dir()
                C.ensure_data_dir()                      # 同一個資料夾只警告一次
            self.assertEqual(buf.getvalue().count('警告'), 1)
            self.assertIn('chmod 700', buf.getvalue())
            self.assertEqual(oct(os.stat(data).st_mode & 0o777), '0o755')     # 不擅自改權限

    def test_silent_when_created_by_program_or_already_700(self):
        with helpers.config() as d:
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                C.ensure_data_dir()
            self.assertEqual(buf.getvalue(), '')
            self.assertEqual(oct(os.stat(os.path.join(d, 'data')).st_mode & 0o777), '0o700')


if __name__ == '__main__':
    unittest.main()
