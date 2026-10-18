"""臺北捷運功能：全部用假 SOAP 伺服器（不連網）。帳密是測試專用的編造值。"""
import contextlib
import io
import json
import os
import stat
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import helpers
import fake_metro as FM
import dash_config as C
import dash_metro as M
import server
from dash_support import APIError, NotConfigured
from test_server import Running, js


@contextlib.contextmanager
def creds(account=FM.GOOD_ACCOUNT, password=FM.GOOD_PASSWORD):
    os.environ['METRO_ACCOUNT'], os.environ['METRO_PASSWORD'] = account, password
    try:
        yield
    finally:
        os.environ.pop('METRO_ACCOUNT', None)
        os.environ.pop('METRO_PASSWORD', None)


@contextlib.contextmanager
def fake(**cfg_extra):
    fm = FM.FakeMetro()
    try:
        with helpers.config(fm.cfg(**cfg_extra)) as d, creds():
            yield fm, d
    finally:
        fm.close()


class ParseRows(unittest.TestCase):
    def test_json_followed_by_empty_envelope(self):
        raw = (json.dumps(FM.TRACK[:2], ensure_ascii=False) + FM.EMPTY_ENVELOPE).encode()
        self.assertEqual(len(M.parse_rows(raw)), 2)

    def test_standard_soap_with_json_string(self):
        raw = FM.soap_wrap('getCarWeightBRInfo', json.dumps(FM.BR_ROWS, ensure_ascii=False)).encode()
        self.assertEqual(M.parse_rows(raw)[0]['StationID'], 'BR24')

    def test_bom_and_whitespace(self):
        self.assertEqual(M.parse_rows(b'\xef\xbb\xbf  \n[{"a": 1}]'), [{'a': 1}])

    def test_non_dict_rows_are_dropped(self):
        self.assertEqual(M.parse_rows(b'[{"a": 1}, "x", 3, null]'), [{'a': 1}])

    def test_fault_is_explained(self):
        raw = ('<?xml version="1.0"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body><soap:Fault>'
               '<faultstring>帳號或密碼錯誤</faultstring></soap:Fault></soap:Body></soap:Envelope>').encode()
        with self.assertRaises(APIError) as cm:
            M.parse_rows(raw)
        self.assertEqual(cm.exception.code, 'metro_auth')               # 看起來是認證問題 → 長冷卻
        self.assertNotIn('帳號或密碼錯誤', str(cm.exception))            # 上游原文不回顯，只有固定訊息

    def test_bad_responses_are_clear_errors_not_crashes(self):
        for raw in (b'', b'garbage text', b'<html><body>err</body></html>', b'[{"a": 1}', b'<a><b></a>', FM.EMPTY_ENVELOPE.encode(),
                    FM.soap_wrap('x', '{"a": 1}').encode(), FM.soap_wrap('x', '[oops').encode(), b'{"a": 1}', b'\xff\xfe\x00\x01',
                    b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><r>&a;</r>'):
            with self.subTest(raw=raw[:30]):
                with self.assertRaises(APIError) as cm:
                    M.parse_rows(raw)
                self.assertEqual(cm.exception.status, 502)
                self.assertIn('臺北捷運 API', str(cm.exception))


class Envelope(unittest.TestCase):
    def test_credentials_are_xml_escaped(self):
        import xml.etree.ElementTree as ET
        evil = ('</passWord><injected>1</injected>', 'a&b<c>"d\'e')
        body = M.build_envelope('getTrackInfo', evil)
        root = ET.fromstring(body)                                  # 一定還是合法 XML
        texts = {el.tag.split('}')[-1]: el.text for el in root.iter()}
        self.assertEqual(texts['userName'], evil[0])                # 還原後與原值完全相同
        self.assertEqual(texts['passWord'], evil[1])
        self.assertNotIn('injected', {el.tag.split('}')[-1] for el in root.iter()})   # 沒有多出元素
        self.assertNotIn(b'<injected>', body)

    def test_station_list_request_has_no_credentials(self):
        self.assertNotIn(b'userName', M.build_envelope('GetStationList'))


class Credentials(unittest.TestCase):
    def test_not_configured_message_points_to_application(self):
        with helpers.config():
            with self.assertRaises(NotConfigured) as cm:
                M.credentials()
            self.assertIn('向臺北捷運公司申請', cm.exception.reason)
            self.assertIn('README', cm.exception.reason)

    def test_env_vars(self):
        with helpers.config(), creds('a', 'b'):
            self.assertEqual(M.credentials(), ('a', 'b'))
        with helpers.config():
            os.environ['METRO_ACCOUNT'] = 'only-account'
            try:
                with self.assertRaises(NotConfigured):             # 只有一個不算
                    M.credentials()
            finally:
                del os.environ['METRO_ACCOUNT']

    def test_file_permissions_and_format(self):
        with helpers.config() as d:
            f = os.path.join(d, 'metro.json')
            with open(f, 'w') as fh:
                json.dump({'account': 'file-acct', 'password': 'file-pw-SECRET', 'items': [1], 'source': 'x'}, fh)
            C.CFG['metro']['credentials_file'] = f
            os.chmod(f, 0o644)
            with self.assertRaises(NotConfigured) as cm:
                M.credentials()
            self.assertIn('chmod 600', cm.exception.reason)
            self.assertNotIn('file-pw-SECRET', cm.exception.reason)
            os.chmod(f, 0o600)
            self.assertEqual(M.credentials(), ('file-acct', 'file-pw-SECRET'))       # 多餘欄位忽略
            for bad in ('{not json', '[]', '{"account": "x"}', json.dumps({'account': 'a\x00b', 'password': 'p'}),
                        json.dumps({'account': 'a', 'password': 'p' * 201}), json.dumps({'account': 5, 'password': 'p'})):
                with open(f, 'w') as fh:
                    fh.write(bad)
                os.chmod(f, 0o600)
                with self.subTest(bad=bad[:20]):
                    with self.assertRaises(NotConfigured) as cm:
                        M.credentials()
                    self.assertNotIn('p' * 201, cm.exception.reason)
            os.unlink(f)
            self.assertRaises(NotConfigured, M.credentials)                           # 檔案不存在＝沒設定

    def test_env_wins_over_file_and_control_chars_rejected(self):
        with helpers.config() as d, creds('env-a', 'env-b'):
            C.CFG['metro']['credentials_file'] = os.path.join(d, 'nofile')
            self.assertEqual(M.credentials(), ('env-a', 'env-b'))
        with helpers.config(), creds('a\nb', 'pw'):
            with self.assertRaises(NotConfigured):
                M.credentials()

    def test_config_never_stores_credentials(self):
        text = open(os.path.join(helpers.ROOT, 'config.example.json'), encoding='utf-8').read()
        self.assertNotIn('password": "', text.replace('"password\\": \\"…\\"', ''))
        cfg = C.load(os.path.join(helpers.ROOT, 'config.example.json'))
        self.assertEqual(cfg['metro']['credentials_file'], '')


class ConfigValidation(unittest.TestCase):
    def load(self, metro):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'c.json')
            with open(p, 'w', encoding='utf-8') as f:
                json.dump({'metro': metro}, f)
            return C.load(p)

    def test_bad_values_fail_at_startup(self):
        for bad in ({'cache_seconds': 29}, {'cache_seconds': 0}, {'cache_seconds': 'x'}, {'cache_seconds': 99999}, {'timeout_seconds': 1},
                    {'nearby_stations': 9}, {'nearby_stations': -1}, {'nearby_stations': True}, {'stations': '台北車站'},
                    {'stations': [''] }, {'stations': ['x' * 21]}, {'stations': ['a'] * 11}, {'stations': [5]},
                    {'max_response_bytes': 10}, {'api_base': 'http://api.example.com/x'}, {'api_base': 'ftp://x'}, {'api_base': ''},
                    {'station_list_url': 'http://metro.example/x'}, {'account_env': 5}, {'credentials_file': 5}):
            with self.subTest(bad=bad), self.assertRaises(C.ConfigError):
                self.load(bad)

    def test_good_values(self):
        cfg = self.load({'cache_seconds': 30, 'stations': ['台北車站', '板橋'], 'api_base': 'http://127.0.0.1:1', 'nearby_stations': 0})
        self.assertEqual(cfg['metro']['stations'], ['台北車站', '板橋'])
        self.load({'api_base': 'https://api.metro.taipei/metroapi/'})


class Arrivals(unittest.TestCase):
    def test_parsing_sorting_and_states(self):
        with fake(stations=['台北車站', '臺北車站', '板橋', '不存在站']) as (fm, _):
            d = M.arrivals()
        st = {s['name']: s for s in d['stations']}
        tp = {x['to']: x['trains'] for x in st['台北車站']['directions']}
        secs = [t['seconds'] for t in tp['淡水站']]
        self.assertAlmostEqual(secs[0], 65 - 5, delta=3)                                   # 依倒數由近到遠；資料已過了約 5 秒，倒數已扣掉
        self.assertAlmostEqual(secs[1], 402 - 5, delta=3)
        self.assertEqual(tp['象山站'][0]['state'], 'arriving')
        self.assertEqual(tp['南港展覽館站'][0]['state'], 'pending')
        self.assertEqual(tp['頂埔站'][0]['state'], 'closed')
        self.assertEqual(tp['新店站'][0]['state'], 'unknown')
        self.assertEqual(st['台北車站'], st['台北車站'])
        self.assertEqual(st['臺北車站']['directions'], st['台北車站']['directions'])       # 臺／台 視為同字
        self.assertEqual(st['板橋']['directions'][0]['trains'][0]['train'], '12')          # 「板橋」對得到「板橋站」
        self.assertFalse(st['不存在站']['found'])

    def test_countdown_parser(self):
        for text, state, sec in (('00:39', 'countdown', 39), ('12:00', 'countdown', 720), ('列車進站', 'arriving', 0), ('資料擷取中', 'pending', None),
                                 ('營運時間已過', 'closed', None), ('', 'unknown', None), ('x' * 50, 'unknown', None), (None, 'unknown', None)):
            r = M.parse_countdown(text)
            self.assertEqual((r['state'], r['seconds']), (state, sec))
            self.assertLessEqual(len(r['text']), 20 if state == 'unknown' else 50)

    def test_requires_credentials_and_stations(self):
        with fake() as (fm, _):
            C.CFG['metro']['stations'] = []
            with self.assertRaises(NotConfigured) as cm:
                M.arrivals()
            self.assertIn('metro.stations', cm.exception.reason)
        with helpers.config({'metro': {'stations': ['台北車站']}}):
            with self.assertRaises(NotConfigured) as cm:
                M.arrivals()
            self.assertIn('申請', cm.exception.reason)

    def test_works_with_standard_soap_format_too(self):
        with fake() as (fm, _):
            fm.mode['/TrackInfo.asmx'] = 'soap'
            self.assertTrue(M.arrivals()['stations'][0]['found'])


class Crowding(unittest.TestCase):
    def test_nearby_filter_levels_and_sorting(self):
        with fake(stations=['台北車站'], nearby_stations=2) as (fm, _):
            d = M.crowding()
        trains = d['stations'][0]['trains']
        by = {t['train']: t for t in trains}
        self.assertEqual(d['stations'][0]['codes'], ['BL12', 'R10'])
        self.assertIn('201', by)                        # R10 本站
        self.assertIn('202', by)                        # R12，後 2 站
        self.assertIn('204', by)                        # BL12 本站（另一條線）
        self.assertNotIn('203', by)                     # R20 太遠
        self.assertNotIn('205', by)                     # 沒有站碼
        self.assertEqual(by['201']['levels'], [1, 2, 3, 1, 9, None])      # 非數字→None，未知等級保留原值
        self.assertEqual(by['201']['offset'], 0)
        self.assertEqual(by['202']['offset'], 2)
        self.assertEqual(by['201']['at_name'], '台北車站')
        self.assertEqual([t['dist'] for t in trains], sorted(t['dist'] for t in trains))
        self.assertIn('依資料推斷', d['level_note'])
        self.assertEqual(d['level_text'], {1: '寬鬆', 2: '適中', 3: '擁擠'})

    def test_nearby_zero_means_only_the_station(self):
        with fake(stations=['台北車站'], nearby_stations=0) as (fm, _):
            self.assertEqual({t['train'] for t in M.crowding()['stations'][0]['trains']}, {'201', '204'})

    def test_wenhu_line(self):
        with fake(stations=['南港展覽館'], nearby_stations=1) as (fm, _):
            d = M.crowding()
            t = d['stations'][0]['trains']
            self.assertEqual([x['train'] for x in t], ['45,42'])
            self.assertEqual(t[0]['line'], '文湖線')
            self.assertEqual(t[0]['levels'], [2, 1, 3, 1])
            self.assertEqual(t[0]['direction'], '下行')
            self.assertEqual(fm.count('/CarWeight.asmx'), 0)           # 只有文湖線的站就不去打高運量
            self.assertEqual(fm.count('/CarWeightBR.asmx'), 1)

    def test_uncovered_and_unknown_stations(self):
        with fake(stations=['環狀線示範站', '不存在的站']) as (fm, _):
            d = M.crowding()
            st = {s['name']: s for s in d['stations']}
            self.assertFalse(st['環狀線示範站']['covered'])                    # 環狀線沒有擁擠度資料
            self.assertEqual(st['環狀線示範站']['trains'], [])
            self.assertFalse(st['不存在的站']['found'])
            self.assertEqual(fm.count('/CarWeight.asmx') + fm.count('/CarWeightBR.asmx'), 0)

    def test_one_source_failing_still_shows_the_other(self):
        with fake(stations=['台北車站', '南港展覽館']) as (fm, _):
            fm.mode['/CarWeightBR.asmx'] = 'garbage'
            d = M.crowding()
            self.assertTrue(any('文湖線' in e for e in d['errors']))
            self.assertTrue(d['stations'][0]['trains'])

    def test_station_list_failure_is_a_502_with_reason(self):
        with fake() as (fm, _):
            fm.mode['/RouteControl.asmx'] = '500'
            with self.assertRaises(APIError) as cm:
                M.crowding()
            self.assertEqual(cm.exception.code, 'metro_http')
            self.assertEqual(fm.count('/CarWeight.asmx'), 0)
            self.assertEqual(M.arrivals()['stations'][0]['found'], True)      # 到站倒數不受影響

    def test_station_list_needs_no_credentials(self):
        with fake() as (fm, _):
            M.station_list()
            req = [r for r in fm.requests if r[0] == '/RouteControl.asmx'][0]
            self.assertIsNone(req[1])
            self.assertIsNone(req[2])

    def test_helpers(self):
        self.assertEqual(M._code('BL12'), ('BL', 12))
        self.assertEqual(M._code('R22A'), ('R', 22))
        for bad in ('ARENA', 'BL07MALL', 'R33_P', '', None, 'x1'):
            self.assertIsNone(M._code(bad))
        self.assertEqual(M.norm(' 臺北車站 '), M.norm('台北車站'))       # 兩邊用同一套處理：「台北車站」本身以「站」結尾也對得上
        self.assertEqual(M.norm('板橋'), M.norm('板橋站'))


class Transport(unittest.TestCase):
    def test_credentials_roundtrip_and_auth_failure(self):
        with fake() as (fm, _):
            M.arrivals()
            req = [r for r in fm.requests if r[0] == '/TrackInfo.asmx'][0]
            self.assertEqual((req[1], req[2]), (FM.GOOD_ACCOUNT, FM.GOOD_PASSWORD))   # 含 & < > " ' 的密碼完整送達
        with helpers.config(FM.FakeMetro().cfg()), creds('wrong', 'wrong'):
            with self.assertRaises(APIError) as cm:
                M.arrivals()
            self.assertEqual(cm.exception.code, 'metro_auth')
            self.assertNotIn('wrong', str(cm.exception))

    def test_redirect_is_refused_and_credentials_not_forwarded(self):
        seen = []

        class Target(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                seen.append(self.rfile.read(int(self.headers.get('Content-Length') or 0)))
                self.send_response(200)
                self.end_headers()
        target = ThreadingHTTPServer(('127.0.0.1', 0), Target)
        threading.Thread(target=target.serve_forever, daemon=True).start()
        try:
            for code in (301, 302, 307, 308):
                with fake() as (fm, _):
                    fm.redirect_to = f'http://127.0.0.1:{target.server_port}/other'
                    with self.subTest(code=code), self.assertRaises(APIError) as cm:
                        M.arrivals()
                    self.assertEqual(cm.exception.code, 'metro_redirect')
        finally:
            target.shutdown()
            target.server_close()
        self.assertEqual(seen, [])                      # 另一個來源完全沒收到請求（更不可能收到帳密）

    def test_timeout(self):
        with fake() as (fm, _):
            C.CFG['metro']['timeout_seconds'] = 0.5
            fm.delay = 1.5
            with self.assertRaises(APIError) as cm:
                M.arrivals()
            self.assertEqual(cm.exception.code, 'metro_timeout')
            self.assertEqual(cm.exception.status, 504)

    def test_unreachable(self):
        with helpers.config({'metro': {'api_base': 'http://127.0.0.1:1', 'stations': ['台北車站']}}), creds():
            with self.assertRaises(APIError) as cm:
                M.arrivals()
            self.assertEqual(cm.exception.code, 'metro_unreachable')

    def test_response_size_limit(self):
        with fake() as (fm, _):
            C.CFG['metro']['max_response_bytes'] = 1000
            fm.mode['/TrackInfo.asmx'] = 'huge'
            with self.assertRaises(APIError) as cm:
                M.arrivals()
            self.assertEqual(cm.exception.code, 'metro_too_large')

    def test_bad_responses_through_the_wire(self):
        for mode, code in (('garbage', 'metro_unknown'), ('html', 'metro_bad_response'), ('empty', 'metro_bad_response'),
                           ('notlist', 'metro_unknown'), ('doctype', 'metro_bad_response'), ('fault', 'metro_auth'), ('500', 'metro_http')):
            with fake() as (fm, _), self.subTest(mode=mode):
                fm.mode['/TrackInfo.asmx'] = mode
                with self.assertRaises(APIError) as cm:
                    M.arrivals()
                self.assertEqual(cm.exception.code, code)
                self.assertNotIn(FM.GOOD_PASSWORD, str(cm.exception))


class Caching(unittest.TestCase):
    def test_server_side_cache_and_expiry(self):
        with fake() as (fm, _):
            clock = [1000.0]
            M._cache.clock = lambda: clock[0]
            try:
                for _ in range(5):
                    M.arrivals()
                self.assertEqual(fm.count('/TrackInfo.asmx'), 1)         # 多個「瀏覽器」只打捷運一次
                clock[0] += 29
                M.arrivals()
                self.assertEqual(fm.count('/TrackInfo.asmx'), 1)
                clock[0] += 2
                M.arrivals()
                self.assertEqual(fm.count('/TrackInfo.asmx'), 2)         # 過了 30 秒才重抓
            finally:
                import time
                M._cache.clock = time.monotonic

    def test_cache_seconds_cannot_be_below_30(self):
        with fake() as (fm, _):
            C.CFG['metro']['cache_seconds'] = 1             # 就算被硬改，程式仍至少快取 30 秒
            M.arrivals()
            M.arrivals()
            self.assertEqual(fm.count('/TrackInfo.asmx'), 1)

    def test_concurrent_requests_hit_metro_once(self):
        with fake() as (fm, _):
            fm.delay = 0.3
            ts = [threading.Thread(target=M.arrivals) for _ in range(10)]
            [t.start() for t in ts]
            [t.join(10) for t in ts]
            self.assertEqual(fm.count('/TrackInfo.asmx'), 1)

    def test_failures_are_remembered_briefly(self):
        with fake() as (fm, _):
            fm.mode['/TrackInfo.asmx'] = 'garbage'
            for _ in range(4):
                with self.assertRaises(APIError):
                    M.arrivals()
            self.assertEqual(fm.count('/TrackInfo.asmx'), 1)         # 出錯期間不重複敲捷運

    def test_wrong_credentials_are_not_resent(self):
        fm = FM.FakeMetro()
        try:
            with helpers.config(fm.cfg()), creds('wrong', 'wrong'):
                for _ in range(5):
                    with self.assertRaises(APIError):
                        M.arrivals()
                self.assertEqual(fm.count('/TrackInfo.asmx'), 1)     # 帳密錯誤只送一次，避免被鎖
        finally:
            fm.close()

    def test_not_configured_is_not_cached(self):
        fm = FM.FakeMetro()
        try:
            with helpers.config(fm.cfg()):
                with self.assertRaises(NotConfigured):
                    M.arrivals()
                with creds():
                    self.assertTrue(M.arrivals()['stations'])          # 補上帳密後立刻可用
        finally:
            fm.close()


class ThroughHttp(unittest.TestCase):
    def test_not_configured_screen_data(self):
        with Running({'metro': {'stations': ['台北車站']}}) as call:
            st, _, raw = call('GET', '/api/metro/arrivals')
            self.assertEqual(st, 200)
            d = js(raw)
            self.assertFalse(d['configured'])
            self.assertIn('向臺北捷運公司申請', d['reason'])
            self.assertEqual(js(call('GET', '/api/metro/crowding')[2])['configured'], False)
            f = js(call('GET', '/api/features')[2])['features']['metro']
            self.assertTrue(f['enabled'])
            self.assertFalse(f['ready'])
            self.assertIn('申請', f['reason'])

    def test_configured_flow_and_no_secret_leak(self):
        fm = FM.FakeMetro()
        log = io.StringIO()
        server.LOG_REQUESTS = True
        try:
            with Running(fm.cfg()) as call, creds(), contextlib.redirect_stderr(log):
                self.assertTrue(js(call('GET', '/api/features')[2])['features']['metro']['ready'])
                bodies = []
                for p in ('/api/metro/arrivals', '/api/metro/crowding', '/api/features', '/api/metro/arrivals'):
                    st, _, raw = call('GET', p)
                    self.assertEqual(st, 200, p)
                    bodies.append(raw.decode())
                self.assertEqual(js(bodies[0].encode())['stations'][0]['name'], '台北車站')
                self.assertTrue(js(bodies[1].encode())['stations'][0]['trains'])
        finally:
            server.LOG_REQUESTS = False
            fm.close()
        everything = ''.join(bodies) + log.getvalue()
        for secret in (FM.GOOD_PASSWORD, FM.GOOD_ACCOUNT):
            self.assertNotIn(secret, everything)

    def test_upstream_errors_are_clear_json_not_500(self):
        fm = FM.FakeMetro()
        fm.mode['/TrackInfo.asmx'] = 'fault'
        fm.mode['/CarWeight.asmx'] = 'garbage'
        try:
            with Running(fm.cfg()) as call, creds():
                st, _, raw = call('GET', '/api/metro/arrivals')
                self.assertEqual(st, 502)
                self.assertIn('臺北捷運 API', js(raw)['error'])
                self.assertEqual(js(raw)['code'], 'metro_auth')
                st, _, raw = call('GET', '/api/metro/crowding')
                self.assertEqual(st, 200)                           # 部分來源壞掉仍回 200，錯誤放在 errors
                self.assertTrue(js(raw)['errors'])
        finally:
            fm.close()

    def test_bad_credentials_file_permissions_show_reason(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, 'm.json')
            with open(f, 'w') as fh:
                json.dump({'account': 'a', 'password': 'p-secret-value'}, fh)
            os.chmod(f, 0o644)
            with Running({'metro': {'credentials_file': f, 'stations': ['台北車站']}}) as call:
                st, _, raw = call('GET', '/api/metro/arrivals')
                self.assertEqual(st, 200)
                self.assertIn('chmod 600', js(raw)['reason'])
                self.assertNotIn('p-secret-value', raw.decode())
                self.assertIn('chmod 600', js(call('GET', '/api/features')[2])['features']['metro']['reason'])

    def test_feature_can_be_disabled(self):
        with Running({'features': {'metro': False}}) as call:
            self.assertEqual(call('GET', '/api/metro/arrivals')[0], 404)

    def test_no_stations_configured(self):
        with Running({'metro': {'stations': []}}) as call, creds():
            d = js(call('GET', '/api/metro/arrivals')[2])
            self.assertFalse(d['configured'])
            self.assertIn('metro.stations', d['reason'])


if __name__ == '__main__':
    unittest.main()
