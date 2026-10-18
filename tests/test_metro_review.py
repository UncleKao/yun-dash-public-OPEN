"""安全複核（N1–N7）的回歸測試：重現當時描述的情境，確認已修。"""
import contextlib
import io
import os
import threading
import time
import unittest

import helpers
import fake_metro as FM
import dash_config as C
import dash_metro as M
import server
from dash_support import APIError
from test_metro import creds, fake
from test_server import Running, js

ROOT = helpers.ROOT


def run_threads(n, fn):
    out, lock = [], threading.Lock()
    barrier = threading.Barrier(n)

    def one():
        barrier.wait()
        try:
            r = ('ok', fn())
        except APIError as e:
            r = ('err', e)
        with lock:
            out.append(r)
    ts = [threading.Thread(target=one) for _ in range(n)]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    return out


class N1_ConcurrentRequestsShareOneUpstreamCall(unittest.TestCase):
    """多個請求同時進來：不論上游成功、認證失敗、逾時或格式錯誤，都只送一次，其餘共用結果／冷卻。"""

    def check(self, fm, expect_code, n=12):
        res = run_threads(n, lambda: M.rows('track'))
        self.assertEqual(len(res), n)
        self.assertEqual(fm.count('/TrackInfo.asmx'), 1, '上游只該被問一次')
        if expect_code:
            self.assertTrue(all(k == 'err' and r.code == expect_code for k, r in res), [(k, getattr(r, 'code', None)) for k, r in res])
            before = fm.count('/TrackInfo.asmx')
            for _ in range(3):
                with self.assertRaises(APIError):
                    M.rows('track')                       # 冷卻期間新請求也不再送
            self.assertEqual(fm.count('/TrackInfo.asmx'), before)
        else:
            self.assertTrue(all(k == 'ok' for k, _ in res))

    def test_success(self):
        with fake() as (fm, _):
            fm.delays['/TrackInfo.asmx'] = 0.4
            self.check(fm, None)

    def test_auth_failure_http_401(self):
        fm = FM.FakeMetro()
        try:
            with helpers.config(fm.cfg()), creds('wrong', 'wrong'):
                fm.delays['/TrackInfo.asmx'] = 0.4
                self.check(fm, 'metro_auth')
        finally:
            fm.close()

    def test_auth_failure_in_result_text(self):
        with fake() as (fm, _):
            fm.mode['/TrackInfo.asmx'] = 'result_text'
            fm.delays['/TrackInfo.asmx'] = 0.4
            self.check(fm, 'metro_auth')

    def test_timeout(self):
        with fake() as (fm, _):
            C.CFG['metro']['timeout_seconds'] = 0.4
            fm.delays['/TrackInfo.asmx'] = 1.0
            self.check(fm, 'metro_timeout')

    def test_bad_format(self):
        with fake() as (fm, _):
            fm.mode['/TrackInfo.asmx'] = 'html'
            fm.delays['/TrackInfo.asmx'] = 0.3
            self.check(fm, 'metro_bad_response')

    def test_reviewer_scenario_all_callers_pass_the_outer_check_first(self):
        """複核報告的重現方式：八個執行緒用 barrier 同時進入，原本會各送一次（共 8 次）。"""
        fm = FM.FakeMetro()
        try:
            with helpers.config(fm.cfg()), creds('wrong', 'wrong'):
                fm.delays['/TrackInfo.asmx'] = 0.3
                res = run_threads(8, lambda: M.rows('track'))
                self.assertEqual(fm.count('/TrackInfo.asmx'), 1)
                self.assertEqual(sum(1 for k, _ in res if k == 'err'), 8)
        finally:
            fm.close()

    def test_different_apis_still_run_in_parallel(self):
        with fake() as (fm, _):
            fm.delays['/TrackInfo.asmx'] = 1.0
            t = threading.Thread(target=lambda: M.rows('track'))
            t.start()
            time.sleep(0.2)
            t0 = time.time()
            M.rows('weight')                                  # 另一支 API 不必等第一支
            self.assertLess(time.time() - t0, 0.6)
            t.join(10)

    def test_auth_cooldown_is_shared_across_apis_and_per_credential(self):
        fm = FM.FakeMetro()
        try:
            with helpers.config(fm.cfg(stations=['台北車站', '南港展覽館'])), creds('wrong', 'wrong'):
                with self.assertRaises(APIError):
                    M.rows('track')
                self.assertEqual(fm.count('/CarWeight.asmx') + fm.count('/CarWeightBR.asmx'), 0)
                for name in ('weight', 'weight_br', 'track'):
                    with self.assertRaises(APIError) as cm:
                        M.rows(name)                            # 同一組錯帳密：其他兩支也不再送
                    self.assertEqual(cm.exception.code, 'metro_auth')
                self.assertEqual(sum(fm.count(p) for p in ('/TrackInfo.asmx', '/CarWeight.asmx', '/CarWeightBR.asmx')), 1)
                with creds():                                  # 換成正確帳密：不受先前冷卻影響
                    self.assertTrue(M.rows('track'))
        finally:
            fm.close()

    def test_cooldown_counts_from_when_the_result_arrives(self):
        fm = FM.FakeMetro()
        try:
            with helpers.config(fm.cfg()), creds('wrong', 'wrong'):
                fm.delays['/TrackInfo.asmx'] = 0.5
                t0 = time.monotonic()
                with self.assertRaises(APIError):
                    M.rows('track')
                until = list(M._auth_fail.values())[0][0]
                self.assertGreaterEqual(until - t0, M.AUTH_FAIL_TTL + 0.4)     # 從收到結果（約 0.5 秒後）起算，不是從發送時
        finally:
            fm.close()

    def test_cooldown_expires(self):
        fm = FM.FakeMetro()
        clock = [1000.0]
        orig = M._mono
        M._mono = lambda: clock[0]
        try:
            with helpers.config(fm.cfg()), creds('wrong', 'wrong'):
                with self.assertRaises(APIError):
                    M.rows('track')
                clock[0] += M.AUTH_FAIL_TTL - 1
                with self.assertRaises(APIError):
                    M.rows('track')
                self.assertEqual(fm.count('/TrackInfo.asmx'), 1)
                clock[0] += 2
                with self.assertRaises(APIError):
                    M.rows('track')
                self.assertEqual(fm.count('/TrackInfo.asmx'), 2)            # 冷卻過了才再試一次
        finally:
            M._mono = orig
            fm.close()

    def test_station_list_failure_is_also_single_flight(self):
        with fake() as (fm, _):
            fm.mode['/RouteControl.asmx'] = 'garbage'
            fm.delays['/RouteControl.asmx'] = 0.3
            res = run_threads(8, M.station_list)
            self.assertEqual(fm.count('/RouteControl.asmx'), 1)
            self.assertTrue(all(k == 'err' for k, _ in res))


class N2_AuthErrorsInsideHttp200(unittest.TestCase):
    CASES = ('result_text', 'plain_text', 'json_string', 'fault')

    def test_each_form_is_classified_as_auth_with_long_cooldown(self):
        for mode in self.CASES:
            with self.subTest(mode=mode), fake() as (fm, _):
                fm.mode['/TrackInfo.asmx'] = mode
                t0 = time.monotonic()
                with self.assertRaises(APIError) as cm:
                    M.arrivals()
                self.assertEqual((cm.exception.code, cm.exception.status), ('metro_auth', 502))
                rec = list(M._auth_fail.values())[0]
                self.assertAlmostEqual(rec[0] - t0, M.AUTH_FAIL_TTL, delta=3)           # 5 分鐘，不是 30 秒
                self.assertEqual(M._fail, {})
                self.assertEqual(M._fail.get('track'), None)

    def test_english_and_other_wordings(self):
        for text in ('Authentication failed', 'invalid password', 'Unauthorized', '登入失敗', '權限不足', 'Access denied'):
            with self.subTest(text=text):
                with self.assertRaises(APIError) as cm:
                    M.parse_rows(FM.soap_wrap('x', text).encode())
                self.assertEqual(cm.exception.code, 'metro_auth')

    def test_unknown_text_is_not_asserted_to_be_a_password_problem(self):
        for mode in ('result_text', 'plain_text', 'json_string'):
            with self.subTest(mode=mode), fake() as (fm, _):
                fm.result_text = '系統維護中，請稍後'
                fm.mode['/TrackInfo.asmx'] = mode
                t0 = time.monotonic()
                with self.assertRaises(APIError) as cm:
                    M.arrivals()
                self.assertEqual(cm.exception.code, 'metro_unknown')
                self.assertNotIn('系統維護', str(cm.exception))                          # 原文不回顯
                self.assertNotIn('密碼', str(cm.exception))
                self.assertEqual(M._auth_fail, {})
                self.assertAlmostEqual(M._fail['track'][0] - t0, M.FAIL_TTL, delta=3)

    def test_legit_empty_data_is_not_an_auth_error(self):
        with fake() as (fm, _):
            fm.mode['/TrackInfo.asmx'] = 'empty_array'
            d = M.arrivals()
            self.assertFalse(d['stations'][0]['found'])             # 合法的空資料：沒有錯誤
            self.assertEqual(M._auth_fail, {})
            fm.mode['/TrackInfo.asmx'] = 'empty'
            M.reset()
            with self.assertRaises(APIError) as cm:
                M.arrivals()
            self.assertEqual(cm.exception.code, 'metro_bad_response')
            self.assertEqual(M._auth_fail, {})

    def test_http_401_403_are_long_cooldown(self):
        fm = FM.FakeMetro()
        try:
            with helpers.config(fm.cfg()), creds('wrong', 'wrong'):
                with self.assertRaises(APIError) as cm:
                    M.rows('track')
                self.assertEqual(cm.exception.code, 'metro_auth')
                self.assertEqual(len(M._auth_fail), 1)
        finally:
            fm.close()

    def test_readme_lists_which_errors_use_the_long_cooldown(self):
        text = open(os.path.join(ROOT, 'README.md'), encoding='utf-8').read()
        for word in ('401', '403', 'SOAP Fault', '5 分鐘'):
            self.assertIn(word, text)


class N3_Freshness(unittest.TestCase):
    def arrivals_with(self, rows, **cfg):
        with fake(**cfg) as (fm, _):
            fm.data['/TrackInfo.asmx'] = rows
            return M.arrivals()

    @staticmethod
    def row(station='台北車站', dest='淡水站', cd='02:00', age=None, **kw):
        r = {'TrainNumber': '', 'StationName': station, 'DestinationName': dest, 'CountDown': cd, 'NowDateTime': '-'}
        if age is not None:
            r['_age'] = age
        r.update(kw)
        return r

    def first(self, d, station=0):
        return d['stations'][station]['directions'][0]['trains'][0]

    def test_old_data_is_marked_stale_not_shown_as_a_countdown(self):
        d = self.arrivals_with([self.row(cd='02:00', NowDateTime='2000-01-01 00:00:00')])        # 複核報告的重現資料
        t = self.first(d)
        self.assertEqual((t['state'], t['seconds'], t['why']), ('stale', None, 'old'))
        self.assertIn('過期', t['text'])
        self.assertTrue(d['stations'][0]['all_stale'])

    def test_countdown_is_reduced_by_the_age_of_the_data(self):
        for age in (0, 30, 100):
            t = self.first(self.arrivals_with([self.row(cd='02:00', age=age)]))
            self.assertEqual(t['state'], 'countdown')
            self.assertAlmostEqual(t['seconds'], 120 - age, delta=2)

    def test_countdown_reaching_zero_becomes_waiting_never_arriving(self):
        t = self.first(self.arrivals_with([self.row(cd='00:20', age=60)]))
        self.assertEqual((t['state'], t['seconds']), ('waiting', 0))
        self.assertEqual(t['text'], '等待更新')

    def test_arriving_is_only_trusted_when_fresh(self):
        self.assertEqual(self.first(self.arrivals_with([self.row(cd='列車進站', age=10)]))['state'], 'arriving')
        self.assertEqual(self.first(self.arrivals_with([self.row(cd='列車進站', age=70)]))['state'], 'stale')

    def test_boundaries(self):
        self.assertEqual(self.first(self.arrivals_with([self.row(cd='09:00', age=M.ARRIVAL_MAX_AGE - 3)]))['state'], 'countdown')
        self.assertEqual(self.first(self.arrivals_with([self.row(cd='09:00', age=M.ARRIVAL_MAX_AGE + 5)]))['state'], 'stale')

    def test_future_missing_and_invalid_times(self):
        t = self.first(self.arrivals_with([self.row(age=-3600)]))
        self.assertEqual((t['state'], t['why']), ('stale', 'future'))
        for bad in ('', 'not-a-time', None, 12345, '2026-13-45 99:99:99'):
            with self.subTest(bad=bad):
                t = self.first(self.arrivals_with([self.row(NowDateTime=bad)]))
                self.assertEqual((t['state'], t['why']), ('stale', 'no_time'))

    def test_mixed_old_and_new_rows_in_one_station(self):
        d = self.arrivals_with([self.row(dest='淡水站', cd='01:00', age=3), self.row(dest='象山站', cd='01:00', age=999)])
        dirs = {x['to']: x['trains'][0] for x in d['stations'][0]['directions']}
        self.assertEqual(dirs['淡水站']['state'], 'countdown')
        self.assertEqual(dirs['象山站']['state'], 'stale')
        self.assertFalse(d['stations'][0]['all_stale'])

    def test_another_stations_fresh_time_does_not_vouch_for_this_one(self):
        rows = [self.row(station='台北車站', cd='01:00', age=999), self.row(station='板橋站', cd='01:00', age=2)]
        d = self.arrivals_with(rows, stations=['台北車站', '板橋'])
        a, b = d['stations']
        self.assertTrue(a['all_stale'])
        self.assertFalse(b['all_stale'])
        self.assertLess(a['updated'], b['updated'])                  # 每個站各自的資料時間
        self.assertNotIn('updated', d)                               # 沒有一個「全部資料」的時間去替舊資料背書

    def test_whole_source_stopped_updating(self):
        with fake() as (fm, _):
            fm.age = 3600                                            # 整批停止更新
            d = M.arrivals()
        self.assertTrue(d['stations'][0]['all_stale'])
        self.assertTrue(all(t['state'] == 'stale' for x in d['stations'][0]['directions'] for t in x['trains']))

    def test_cached_rereads_keep_counting_down(self):
        with fake() as (fm, _):
            fm.data['/TrackInfo.asmx'] = [self.row(cd='02:00', age=0)]
            clock = [None]
            real = M._wall
            base = real()
            M._wall = lambda: base + __import__('datetime').timedelta(seconds=clock[0] or 0)
            try:
                clock[0] = 0
                a = self.first(M.arrivals())['seconds']
                clock[0] = 20
                b = self.first(M.arrivals())['seconds']              # 仍在快取內（沒有重新向上游要資料），但倒數已扣掉 20 秒
                self.assertEqual(fm.count('/TrackInfo.asmx'), 1)
                self.assertAlmostEqual(a - b, 20, delta=1)
                clock[0] = 125
                self.assertEqual(self.first(M.arrivals())['state'], 'stale')       # 資料超過 2 分鐘 → 過期
            finally:
                M._wall = real

    def test_crowding_hides_old_and_invalid_times(self):
        rows = [{'TrainNumber': '1', 'CN1': 'a', 'CID': '1', 'StationID': 'R10', 'Cart1L': '1', 'Cart2L': '1', 'Cart3L': '1', 'Cart4L': '1', 'Cart5L': '1', 'Cart6L': '1', 'utime': '-', '_age': 20},
                {'TrainNumber': '2', 'CN1': 'b', 'CID': '1', 'StationID': 'R10', 'Cart1L': '3', 'Cart2L': '3', 'Cart3L': '3', 'Cart4L': '3', 'Cart5L': '3', 'Cart6L': '3', 'utime': '-', '_age': 3600},
                {'TrainNumber': '3', 'CN1': 'c', 'CID': '1', 'StationID': 'R10', 'Cart1L': '3', 'Cart2L': '3', 'Cart3L': '3', 'Cart4L': '3', 'Cart5L': '3', 'Cart6L': '3', 'utime': 'garbage'},
                {'TrainNumber': '4', 'CN1': 'd', 'CID': '1', 'StationID': 'R10', 'Cart1L': '3', 'Cart2L': '3', 'Cart3L': '3', 'Cart4L': '3', 'Cart5L': '3', 'Cart6L': '3', 'utime': '-', '_age': -7200}]
        with fake() as (fm, _):
            fm.data['/CarWeight.asmx'] = rows
            d = M.crowding()
        st = d['stations'][0]
        self.assertEqual([t['train'] for t in st['trains']], ['1'])
        self.assertEqual(st['stale_hidden'], 3)
        self.assertAlmostEqual(st['trains'][0]['age'], 20, delta=3)

    def test_crowding_wenhu_stale_hidden(self):
        with fake(stations=['南港展覽館']) as (fm, _):
            fm.age = 5000
            d = M.crowding()
        self.assertEqual(d['stations'][0]['trains'], [])
        self.assertGreater(d['stations'][0]['stale_hidden'], 0)

    def test_policy_is_documented_as_local_policy(self):
        text = open(os.path.join(ROOT, 'README.md'), encoding='utf-8').read()
        self.assertIn('本地策略', text)
        self.assertIn('2 分鐘', text)


class N8_ServerGivesExpiryForTheFrontend(unittest.TestCase):
    """前端要能在沒有新回應時自己撤下過期的值，所以後端每筆都要給「還能新鮮幾秒」。"""

    def test_expires_in_for_each_kind_of_arrival(self):
        rows = [N3_Freshness.row(dest='淡水站', cd='05:00', age=30), N3_Freshness.row(dest='象山站', cd='列車進站', age=10),
                N3_Freshness.row(dest='頂埔站', cd='資料擷取中', age=100), N3_Freshness.row(dest='新店站', cd='02:00', age=999)]
        with fake() as (fm, _):
            fm.data['/TrackInfo.asmx'] = rows
            d = M.arrivals()
        t = {x['to']: x['trains'][0] for x in d['stations'][0]['directions']}
        self.assertAlmostEqual(t['淡水站']['expires_in'], M.ARRIVAL_MAX_AGE - 30, delta=2)
        self.assertAlmostEqual(t['象山站']['expires_in'], M.ARRIVING_MAX_AGE - 10, delta=2)        # 進站的期限比較短
        self.assertAlmostEqual(t['頂埔站']['expires_in'], M.ARRIVAL_MAX_AGE - 100, delta=2)
        self.assertNotIn('expires_in', t['新店站'])                                                   # 已經過期的沒有
        self.assertEqual(t['新店站']['state'], 'stale')

    def test_expires_in_is_never_negative_and_waiting_has_one(self):
        with fake() as (fm, _):
            fm.data['/TrackInfo.asmx'] = [N3_Freshness.row(cd='00:10', age=60)]
            t = M.arrivals()['stations'][0]['directions'][0]['trains'][0]
        self.assertEqual(t['state'], 'waiting')
        self.assertGreaterEqual(t['expires_in'], 0)
        self.assertAlmostEqual(t['expires_in'], M.ARRIVAL_MAX_AGE - 60, delta=2)

    def test_crowding_trains_carry_expires_in(self):
        rows = [{'TrainNumber': '1', 'CN1': 'a', 'CID': '1', 'StationID': 'R10', 'Cart1L': '1', 'Cart2L': '1', 'Cart3L': '1', 'Cart4L': '1', 'Cart5L': '1',
                 'Cart6L': '1', 'utime': '-', '_age': 100}]
        with fake(stations=['台北車站', '南港展覽館']) as (fm, _):
            fm.data['/CarWeight.asmx'] = rows
            d = M.crowding()
        t = d['stations'][0]['trains'][0]
        self.assertAlmostEqual(t['expires_in'], M.CROWD_MAX_AGE - 100, delta=2)
        self.assertEqual(d['max_age'], M.CROWD_MAX_AGE)
        wenhu = d['stations'][1]['trains']
        self.assertTrue(all(0 <= x['expires_in'] <= M.CROWD_MAX_AGE for x in wenhu))


class N4_UpstreamFaultTextIsNeverEchoed(unittest.TestCase):
    MARKER = 'SYNTH-SECRET-' + 'x9Qz7'
    PAYLOADS = ('Server error for user ' + MARKER, MARKER + '</passWord><userName>' + MARKER + '</userName>', 'bad\x1b[31m\x07\x08' + MARKER + '\r\nX: y',
                '帳號 ' + MARKER + ' 密碼 ' + MARKER)

    def test_fault_text_not_in_exception_response_or_logs(self):
        for payload in self.PAYLOADS:
            for mode in ('fault_text', 'fault_other'):
                with self.subTest(payload=payload[:20], mode=mode):
                    fm = FM.FakeMetro()
                    fm.fault_text = payload.replace('&', '&amp;').replace('<', '&lt;')
                    fm.mode['/TrackInfo.asmx'] = mode
                    log = io.StringIO()
                    server.LOG_REQUESTS = True
                    try:
                        with Running(fm.cfg()) as call, creds(), contextlib.redirect_stderr(log):
                            st, h, raw = call('GET', '/api/metro/arrivals')
                    finally:
                        server.LOG_REQUESTS = False
                        fm.close()
                    self.assertEqual(st, 502)
                    body = raw.decode()
                    everything = body + log.getvalue() + str(sorted(h.items()))
                    self.assertNotIn(self.MARKER, everything)
                    for ch in ('\x1b', '\x07', '\x08'):
                        self.assertNotIn(ch, everything)
                    self.assertIn(js(raw)['code'], ('metro_auth', 'metro_fault', 'metro_bad_response'))      # 仍可判斷故障類別（含控制字元的不是合法 XML）

    def test_fault_text_not_kept_in_failure_cache(self):
        fm = FM.FakeMetro()
        fm.fault_text = 'xx ' + self.MARKER
        fm.mode['/TrackInfo.asmx'] = 'fault_other'
        try:
            with helpers.config(fm.cfg()), creds():
                with self.assertRaises(APIError):
                    M.rows('track')
                self.assertNotIn(self.MARKER, repr(M._fail) + repr(M._auth_fail))
        finally:
            fm.close()

    def test_non_auth_fault_is_a_fixed_message_with_short_cooldown(self):
        with fake() as (fm, _):
            fm.fault_text = 'Object reference not set to an instance of an object'
            fm.mode['/TrackInfo.asmx'] = 'fault_other'
            with self.assertRaises(APIError) as cm:
                M.rows('track')
            self.assertEqual(cm.exception.code, 'metro_fault')
            self.assertNotIn('Object reference', str(cm.exception))
            self.assertEqual(M._auth_fail, {})

    def test_plain_text_and_result_text_are_not_echoed_either(self):
        for mode in ('plain_text', 'result_text', 'json_string'):
            with self.subTest(mode=mode), fake() as (fm, _):
                fm.result_text = 'weird ' + self.MARKER
                fm.mode['/TrackInfo.asmx'] = mode
                with self.assertRaises(APIError) as cm:
                    M.rows('track')
                self.assertNotIn(self.MARKER, str(cm.exception))


class N5_SameStationSameDestinationAcrossLines(unittest.TestCase):
    def test_two_rows_same_destination_are_flagged_not_called_next_next(self):
        rows = [{'TrainNumber': '', 'StationName': '台北車站', 'DestinationName': '南港展覽館站', 'CountDown': '01:00', 'NowDateTime': '-'},
                {'TrainNumber': '', 'StationName': '台北車站', 'DestinationName': '南港展覽館站', 'CountDown': '04:00', 'NowDateTime': '-'},
                {'TrainNumber': '', 'StationName': '台北車站', 'DestinationName': '淡水站', 'CountDown': '02:00', 'NowDateTime': '-'}]
        with fake() as (fm, _):
            fm.data['/TrackInfo.asmx'] = rows
            d = M.arrivals()
        dirs = {x['to']: x for x in d['stations'][0]['directions']}
        self.assertTrue(dirs['南港展覽館站']['ambiguous_line'])
        self.assertEqual(dirs['南港展覽館站']['count'], 2)
        self.assertFalse(dirs['淡水站']['ambiguous_line'])
        self.assertIn('路線', d['note'])

    def test_frontend_and_readme_say_line_not_distinguished(self):
        js_src = open(os.path.join(ROOT, 'static', 'app.js'), encoding='utf-8').read()
        self.assertIn('路線未區分', js_src)
        readme = open(os.path.join(ROOT, 'README.md'), encoding='utf-8').read()
        self.assertNotIn('同方向有第二筆才會顯示「下下班」', readme)
        self.assertIn('路線未區分', readme)


class N6_ReadmeRateWording(unittest.TestCase):
    def test_rate_is_per_api(self):
        text = open(os.path.join(ROOT, 'README.md'), encoding='utf-8').read()
        self.assertNotIn('捷運每 30 秒最多被問一次', text)
        self.assertIn('每支', text)
        self.assertIn('車站代碼表', text)

    def test_single_process_and_failure_cases_stated(self):
        text = open(os.path.join(ROOT, 'README.md'), encoding='utf-8').read()
        self.assertIn('單一', text)
        self.assertIn('並行', text)

    def test_guarantee_actually_holds_per_api(self):
        with fake() as (fm, _):
            for _ in range(5):
                M.rows('track')
                M.rows('weight')
            self.assertEqual((fm.count('/TrackInfo.asmx'), fm.count('/CarWeight.asmx')), (1, 1))        # 每支各 1 次


if __name__ == '__main__':
    unittest.main()
