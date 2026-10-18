import datetime
import os
import random
import sys
import unittest

import helpers

sys.path.insert(0, os.path.join(helpers.ROOT, 'tools'))
import dash_config as C
import dash_food as F
import dash_validate as V
import food_import
from dash_support import NotConfigured

SAMPLE = os.path.join(helpers.ROOT, 'data.example', 'places.sample.csv')
TZ8 = datetime.timezone(datetime.timedelta(hours=8))


def at(weekday, hh, mm):
    """2030-01-07 是星期一；回傳該週指定星期（0=一）的時刻。"""
    return datetime.datetime(2030, 1, 7 + weekday, hh, mm, tzinfo=TZ8)


class Hours(unittest.TestCase):
    def test_ranges_and_sets(self):
        t = F.parse_hours('一~五 11:00-14:00,17:00-21:00; 六日 10:00-22:00')
        self.assertEqual(t[0], [(660, 840), (1020, 1260)])
        self.assertEqual(t[5], [(600, 1320)])
        self.assertEqual(sorted(t), [0, 1, 2, 3, 4, 5, 6])

    def test_everyday_closed_and_24h(self):
        self.assertEqual(sorted(F.parse_hours('每天 08:00-18:00')), [0, 1, 2, 3, 4, 5, 6])
        self.assertEqual(F.parse_hours('日 公休'), {6: []})
        self.assertEqual(F.parse_hours('每天 24小時')[3], [(0, 1440)])

    def test_unreadable_is_unknown_not_open(self):
        for raw in ('', None, '看心情', '營業中'):
            self.assertEqual(F.parse_hours(raw), {})
            self.assertIsNone(F.is_open(raw, at(0, 12, 0)))

    def test_is_open_boundaries(self):
        h = '一~五 11:00-14:00'
        self.assertTrue(F.is_open(h, at(0, 11, 0)))
        self.assertTrue(F.is_open(h, at(0, 13, 59)))
        self.assertFalse(F.is_open(h, at(0, 14, 0)))
        self.assertFalse(F.is_open(h, at(5, 12, 0)))

    def test_crossing_midnight(self):
        h = '一~日 18:00-02:00'
        self.assertTrue(F.is_open(h, at(2, 23, 0)))
        self.assertTrue(F.is_open(h, at(3, 1, 30)))     # 前一天的營業時段延續到凌晨
        self.assertFalse(F.is_open(h, at(3, 2, 0)))
        self.assertFalse(F.is_open(h, at(3, 10, 0)))


class Tier(unittest.TestCase):
    def test_boundaries(self):
        with helpers.config():
            self.assertEqual(F.tier_of(399), 'daily')
            self.assertEqual(F.tier_of(400), 'mid')
            self.assertEqual(F.tier_of(999), 'mid')
            self.assertEqual(F.tier_of(1000), 'high')
            self.assertEqual(F.tier_of(None), 'daily')


class Pipeline(unittest.TestCase):
    """用範例 CSV 建庫，再跑骰子／附近／分類（全部在臨時資料夾）。"""

    def setUp(self):
        self.cm = helpers.config({'places': {'車站': {'lat': 25.0478, 'lon': 121.5170, 'radius': 100}}})
        self.cm.__enter__()
        self.db = F.init_db()
        self.counts = food_import.import_csv(SAMPLE, self.db)
        self.db.close()

    def tearDown(self):
        self.cm.__exit__(None, None, None)

    def test_import_counts_and_idempotent(self):
        self.assertEqual(self.counts, (12, 0, 0))
        db = F.init_db()
        self.assertEqual(food_import.import_csv(SAMPLE, db), (0, 12, 0))
        db.close()
        self.assertEqual(F.stats()['places'], 12)

    def test_missing_db_is_not_configured(self):
        os.unlink(F.db_path())
        for fn in (F.stats, F.districts, F.ate):
            with self.assertRaises(NotConfigured):
                fn()

    def test_cats_and_tiers(self):
        c = F.cats({'near': 'all'})
        self.assertEqual(c['total'], 12)
        self.assertEqual(c['tiers']['all'], 12)
        self.assertGreaterEqual(c['tiers']['high'], 1)
        only_high = F.cats({'near': 'all', 'tier': 'high'})
        self.assertEqual(only_high['total'], c['tiers']['high'])

    def test_list_filters(self):
        self.assertEqual(F.listing({'near': 'all', 'q': '咖啡'})['total'], 1)
        self.assertEqual(F.listing({'near': 'all', 'cat': '日式'})['total'], 2)
        self.assertEqual(F.listing({'near': 'all', 'district': '另一示範區'})['total'], 2)

    def test_near_uses_center_and_radius(self):
        near = F.near({'near': '車站', 'radius': '300'})
        self.assertEqual(near['radius'], 300)
        self.assertTrue(all(r['dist'] <= 300 for r in near['rows']))
        self.assertEqual([r['dist'] for r in near['rows']], sorted(r['dist'] for r in near['rows']))
        far = F.listing({'near': '車站', 'radius': '100000'})
        self.assertEqual(far['total'], 12)

    def test_unknown_center_is_invalid(self):
        with self.assertRaises(V.Invalid):
            F.cats({'near': '不存在的地點'})

    def test_gps_without_data_falls_back_to_all(self):
        ctr, note = F.resolve_center('gps')
        self.assertIsNone(ctr)
        self.assertIn('不限距離', note)
        with self.assertRaises(NotConfigured):
            F.near({'near': 'gps'})

    def test_dice_excludes_drinks_unless_asked(self):
        for seed in range(30):
            r = F.dice({'place': 'all', 'open': '0'}, random.Random(seed))
            self.assertNotIn(r['pick']['cat'], ('飲料', '咖啡', '甜點'))
        cats = {F.dice({'place': 'all', 'open': '0', 'drinks': '1'}, random.Random(s))['pick']['cat'] for s in range(80)}
        self.assertTrue(cats & {'飲料', '咖啡', '甜點'})

    def test_dice_price_cat_and_empty_pool(self):
        r = F.dice({'place': 'all', 'open': '0', 'price': '100', 'drinks': '1'}, random.Random(1))
        self.assertLessEqual(r['pick']['price'], 100)
        r = F.dice({'place': 'all', 'open': '0', 'cat': '日式'}, random.Random(1))
        self.assertEqual(r['pick']['cat'], '日式')
        r = F.dice({'place': 'all', 'cat': '不存在的類別'}, random.Random(1))
        self.assertIsNone(r['pick'])
        self.assertEqual(r['pool'], 0)

    def test_mark_tried(self):
        pid = F.listing({'near': 'all', 'q': '牛肉麵'})['rows'][0]['id']
        res = F.mark_tried({'id': pid, 'date': '2030-01-05', 'rating': 4.5})
        self.assertEqual(res['date'], '2030-01-05')
        ate = F.ate()
        self.assertEqual(ate['total'], 1)
        self.assertEqual(ate['rows'][0]['mine'], 4.5)
        # 再匯入不會洗掉吃過紀錄
        db = F.init_db()
        food_import.import_csv(SAMPLE, db)
        db.close()
        self.assertEqual(F.ate()['total'], 1)

    def test_mark_tried_rejects_bad_input(self):
        for body in ({}, {'id': 'x'}, {'id': 0}, {'id': 1, 'date': '2030-13-01'}, {'id': 1, 'rating': 9}):
            with self.subTest(body=body), self.assertRaises(V.Invalid):
                F.mark_tried(body)
        from dash_support import APIError
        with self.assertRaises(APIError) as cm:
            F.mark_tried({'id': 99999})
        self.assertEqual(cm.exception.status, 404)

    def test_nearest_shop_helper(self):
        shop = F.nearest_shop(25.0482, 121.5175, 60)
        self.assertEqual(shop['name'], '範例牛肉麵')
        self.assertIsNone(F.nearest_shop(10.0, 10.0, 60))


class BoundingBox(unittest.TestCase):
    """高緯度（倫敦、東京、首爾…）的搜尋框不能比要求的半徑窄。"""

    def test_box_covers_requested_radius_at_every_latitude(self):
        import dash_geo as G
        for lat in (0, 25, 35, 37.5, 51.5, 60, 70):
            for meters in (100, 800, 1500):
                dlat, dlon = F._box(lat, meters)
                east = G.dist_m(lat, 0, lat, dlon)
                north = G.dist_m(lat, 0, lat + dlat, 0)
                with self.subTest(lat=lat, meters=meters):
                    self.assertGreaterEqual(east, meters)
                    self.assertGreaterEqual(north, meters)

    def test_london_shop_699m_east_is_found(self):
        import dash_geo as G
        import math
        lat0, lon0 = 51.5074, -0.1278
        dlon = 699 / (111320 * math.cos(math.radians(lat0)))
        with helpers.config({'places': {'中心': {'lat': lat0, 'lon': lon0}}}):
            db = F.init_db()
            db.execute("insert into places(name,category,address,lat,lng,price) values ('正東店','x','a',?,?,100)", (lat0, lon0 + dlon))
            db.commit()
            db.close()
            true_dist = G.dist_m(lat0, lon0, lat0, lon0 + dlon)
            self.assertLess(true_dist, 800)
            self.assertEqual(F.listing({'near': '中心', 'radius': '800'})['total'], 1)
            self.assertEqual(F.near({'near': '中心', 'radius': '800'})['total'], 1)
            self.assertEqual(F.nearest_shop(lat0, lon0, 800)['name'], '正東店')


class CsvImportEdgeCases(unittest.TestCase):
    def test_non_finite_and_overflow_numbers_are_ignored(self):
        import tempfile
        with helpers.config():
            with tempfile.TemporaryDirectory() as d:
                p = os.path.join(d, 'x.csv')
                with open(p, 'w', encoding='utf-8') as f:
                    f.write('name,price,rating,reviews,lat,lng\n'
                            '壞數字店,inf,nan,1e400,-inf,1e999\n'
                            '好店,120,4.5,300,25.01,121.5\n'
                            '逗號店,"1,200",4,"2,000",25.02,121.51\n')
                db = F.init_db()
                self.assertEqual(food_import.import_csv(p, db), (3, 0, 0))
                db.close()
            rows = {r['name']: r for r in F.listing({'near': 'all'})['rows']}
            self.assertIsNone(rows['壞數字店']['price'])
            self.assertIsNone(rows['壞數字店']['rating'])
            self.assertIsNone(rows['壞數字店']['lat'])
            self.assertEqual(rows['好店']['price'], 120)
            self.assertEqual(rows['逗號店']['price'], 1200)
            import json
            json.dumps(F.listing({'near': 'all'}), allow_nan=False)    # 不會輸出非法的 Infinity／NaN


class WithoutDb(unittest.TestCase):
    def test_nearest_shop_without_db_returns_none(self):
        with helpers.config():
            self.assertIsNone(F.nearest_shop(25.0, 121.5))


if __name__ == '__main__':
    unittest.main()
