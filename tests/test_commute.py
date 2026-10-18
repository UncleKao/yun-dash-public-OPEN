import datetime
import os
import sqlite3
import sys
import time
import unittest

import helpers

sys.path.insert(0, os.path.join(helpers.ROOT, 'tools'))
import dash_commute as K
import dash_config as C
import loc_collect
from dash_support import NotConfigured

TZ8 = datetime.timezone(datetime.timedelta(hours=8))
LANDMARK_A = (25.0330, 121.5654)   # 公開地標座標，純粹示意


def pt(hh, mm, lat, lon, acc=10, ss=0):
    """mm 可以超過 59（會進位到下一小時）。"""
    t = datetime.datetime(2030, 1, 7, hh, 0, ss, tzinfo=TZ8) + datetime.timedelta(minutes=mm)
    return {'dt': t, 'lat': lat, 'lon': lon, 'acc': acc, 'src': 'main'}


def walk_line(start_min, n, lat0, lon0, dlon):
    """每分鐘一點，往東移動 dlon 度。"""
    return [pt((start_min + i) // 60, (start_min + i) % 60, lat0, lon0 + i * dlon) for i in range(n)]


class Segments(unittest.TestCase):
    def setUp(self):
        self.cm = helpers.config({'places': {'家': {'lat': LANDMARK_A[0], 'lon': LANDMARK_A[1], 'radius': 70, 'icon': '🏠'}}})
        self.cm.__enter__()

    def tearDown(self):
        self.cm.__exit__(None, None, None)

    def test_empty(self):
        self.assertEqual(K.build_segments([]), [])

    def test_stay_move_stay(self):
        home = [pt(8, m, LANDMARK_A[0], LANDMARK_A[1] + m * 1e-6) for m in range(0, 30)]
        # 步行：1.3 公里約 15 分鐘 → 約 5 km/h；0.0001 度經度約 10 公尺
        walking = walk_line(8 * 60 + 30, 15, LANDMARK_A[0], LANDMARK_A[1], 0.0009)
        dest_lon = walking[-1]['lon']
        dest = [pt(8, 45 + m, LANDMARK_A[0], dest_lon + m * 1e-6) for m in range(0, 20)]
        segs = K.build_segments(home + walking + dest)
        kinds = [s['kind'] for s in segs]
        self.assertEqual(kinds, ['stay', 'move', 'stay'])
        self.assertEqual(segs[0]['place'], '家')
        self.assertEqual(segs[0]['icon'], '🏠')
        self.assertEqual(segs[1]['mode'], 'walk')
        self.assertTrue(segs[2]['place'][0].isdigit())            # 沒有命名的地方顯示座標

    def test_vehicle_speed(self):
        stay1 = [pt(9, m, LANDMARK_A[0], LANDMARK_A[1]) for m in range(0, 15)]
        ride = [pt(9, 15 + i, LANDMARK_A[0], LANDMARK_A[1] + (i + 1) * 0.005) for i in range(10)]   # 每分鐘約 480 公尺＝約 29 km/h
        stay2 = [pt(9, 26 + m, LANDMARK_A[0], ride[-1]['lon']) for m in range(0, 15)]
        segs = K.build_segments(stay1 + ride + stay2)
        move = [s for s in segs if s['kind'] == 'move'][0]
        self.assertEqual(move['mode'], 'vehicle')
        self.assertGreater(move['kmh'], 20)

    def test_short_pause_is_not_a_stay(self):
        a = [pt(10, m, LANDMARK_A[0], LANDMARK_A[1]) for m in range(0, 20)]
        pause = [pt(10, 20 + m, LANDMARK_A[0], LANDMARK_A[1] + 0.003) for m in range(0, 3)]    # 只停 3 分鐘
        b = [pt(10, 25 + m, LANDMARK_A[0], LANDMARK_A[1] + 0.02 + m * 0.0005) for m in range(0, 20)]
        segs = K.build_segments(a + pause + b)
        self.assertTrue(all(s.get('mins', 99) != 3 for s in segs if s['kind'] == 'stay'))

    def test_shop_name_used_when_no_anchor(self):
        far = [pt(12, m, 25.1, 121.6) for m in range(0, 12)]
        segs = K.build_segments(far, nearest_shop=lambda la, lo: {'name': '範例麵店', 'dist': 12})
        self.assertEqual(segs[0]['place'], '範例麵店(12m)')

    def test_classify(self):
        self.assertEqual(K.classify(1.9), 'slow')
        self.assertEqual(K.classify(2), 'walk')
        self.assertEqual(K.classify(7.9), 'walk')
        self.assertEqual(K.classify(8), 'vehicle')


class Database(unittest.TestCase):
    def test_not_configured_paths(self):
        with helpers.config():
            with self.assertRaises(NotConfigured):
                K.track('2030-01-07')          # 沒設定 entities
        with helpers.config({'commute': {'entities': ['device_tracker.a']}}):
            with self.assertRaises(NotConfigured):
                K.track('2030-01-07')          # 有 entities 但沒有資料庫

    def test_track_reads_collected_rows(self):
        with helpers.config({'commute': {'entities': ['device_tracker.a', 'device_tracker.b']}}) as d:
            C.ensure_data_dir()
            db = sqlite3.connect(K.db_path())
            db.executescript(loc_collect.SCHEMA)
            base = datetime.datetime(2030, 1, 7, 9, 0, tzinfo=TZ8)
            rows = []
            for i in range(20):
                t = base + datetime.timedelta(minutes=i)
                rows.append(('device_tracker.a', t.isoformat(), int(t.timestamp()), 'home', 25.0, 121.5, 10))
            rows.append(('device_tracker.b', base.isoformat(), int(base.timestamp()), 'home', 25.0, 121.5, 10))
            rows.append(('device_tracker.a', (base + datetime.timedelta(hours=1)).isoformat(), int(base.timestamp()) + 3600, 'x', 25.0, 121.5, 500))   # 誤差太大
            rows.append(('device_tracker.a', (base + datetime.timedelta(hours=2)).isoformat(), int(base.timestamp()) + 7200, 'x', None, None, None))    # 沒座標
            db.executemany('INSERT INTO locations VALUES (?,?,?,?,?,?,?)', rows)
            db.commit()
            db.close()
            t = K.track('2030-01-07')
            self.assertEqual(len(t['points']), 21)
            self.assertEqual({p['src'] for p in t['points']}, {'main', 'sub'})
            self.assertEqual(t['segs'][0]['kind'], 'stay')
            self.assertIsNone(t['now'])                       # 不是今天
            self.assertEqual(K.track('2030-01-08')['points'], [])
            self.assertIsNone(K.latest_point(10))             # 2030 年的點不是「最近」

    def test_collector_dedupes_and_prunes(self):
        import dash_ha
        with helpers.config({'commute': {'entities': ['device_tracker.a'], 'retention_days': 1},
                             'ha': {'url': 'http://ha.invalid'}}) as d:
            calls = []
            orig = dash_ha.state
            dash_ha.state = lambda ent: (calls.append(ent), {'state': 'home', 'last_updated': '2030-01-07T01:00:00+00:00',
                                                           'attributes': {'latitude': 25.0, 'longitude': 121.5, 'gps_accuracy': 8}})[1]
            try:
                db, n1 = loc_collect.collect(C.data_path('locations.sqlite'))
                db.close()
                db, n2 = loc_collect.collect(C.data_path('locations.sqlite'))
                self.assertEqual((n1, n2), (1, 0))             # 同一筆定位不重複寫
                db.execute('UPDATE locations SET poll_ts=?', (int(time.time()) - 5 * 86400,))
                loc_collect.prune(db)
                self.assertEqual(db.execute('select count(*) from locations').fetchone()[0], 0)
                db.close()
            finally:
                dash_ha.state = orig


if __name__ == '__main__':
    unittest.main()
