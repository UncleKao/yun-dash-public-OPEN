"""N7：圖表降點不能改掉極值與最後一點。"""
import datetime
import unittest

import helpers
import fake_ha
import dash_custom as K
from test_custom import Env, st, UTC


def rows(values, hours=24):
    now = datetime.datetime.now(UTC)
    n = len(values)
    return [(fake_ha.iso(now - datetime.timedelta(hours=hours) + datetime.timedelta(hours=hours * (i + 1) / (n + 1))), str(v)) for i, v in enumerate(values)]


class ChartExtremes(unittest.TestCase):
    def render(self, values):
        card = {'type': 'chart', 'title': 'c', 'entity': 'sensor.t'}
        with Env({'sensor.t': st('1')}, {'sensor.t': rows(values)} if values else {}, [card]):
            return K.render()['cards'][0]['chart']

    def test_single_spike_and_dip_survive_downsampling(self):
        vals = [10.0] * 3000
        vals[1234], vals[2222] = 999.0, -50.0
        vals[-1] = 7.5
        ch = self.render(vals)
        ys = [p[1] for p in ch['points']]
        self.assertLessEqual(len(ch['points']), K.MAX_POINTS)
        self.assertIn(999.0, ys)                                  # 尖峰還在畫面上，沒有被平均掉
        self.assertIn(-50.0, ys)
        self.assertEqual((ch['max'], ch['min']), (999.0, -50.0))
        self.assertEqual(ch['count'], 3000)

    def test_last_point_is_the_exact_original_value(self):
        vals = [float(i % 40) for i in range(2999)] + [33.25]
        ch = self.render(vals)
        self.assertEqual(ch['last'], 33.25)
        self.assertEqual(ch['points'][-1][1], 33.25)              # 畫面上最後一點也是原值，不是最後一桶的平均
        self.assertEqual(ch['points'][0][1], vals[0])

    def test_stats_come_from_raw_data_even_with_many_points(self):
        vals = [5.0] * 1000
        vals[500] = 6.0
        ch = self.render(vals)
        self.assertEqual((ch['min'], ch['max'], ch['last']), (5.0, 6.0, 5.0))

    def test_small_series_is_untouched(self):
        ch = self.render([1, 2, 3])
        self.assertEqual([p[1] for p in ch['points']], [1, 2, 3])
        self.assertEqual((ch['min'], ch['max'], ch['last']), (1, 3, 3))

    def test_points_stay_in_time_order_and_unique(self):
        vals = [(i * 37) % 101 for i in range(2500)]
        ch = self.render(vals)
        ts = [p[0] for p in ch['points']]
        self.assertEqual(ts, sorted(ts))
        self.assertEqual(len(ts), len(set(ts)))

    def test_empty_series(self):
        ch = self.render([])
        self.assertEqual((ch['points'], ch['min'], ch['max'], ch['last']), ([], None, None, None))


if __name__ == '__main__':
    unittest.main()
