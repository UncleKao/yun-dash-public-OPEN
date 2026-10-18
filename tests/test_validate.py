#!/usr/bin/env python3
"""dash_validate 的輸入驗證測試：每支都測合法、邊界、不合法。不連網、不讀任何真實資料。

跑法：python3 -m unittest discover -s tests
"""
import email
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dash_validate as V   # noqa: E402

Invalid = V.Invalid


def headers(raw):
    """用真的 email.message 當 HTTP 標頭（跟 BaseHTTPRequestHandler.headers 同一種介面）。"""
    return email.message_from_string(raw)




# ── dash_validate：每支都測合法、邊界、不合法 ─────────────────────────────────
class ContentLength(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(V.content_length(headers('Content-Length: 123')), 123)

    def test_missing_header_is_zero(self):
        self.assertEqual(V.content_length(headers('Host: x')), 0)

    def test_boundary_equal_to_limit_passes(self):
        self.assertEqual(V.content_length(headers('Content-Length: 1024'), limit=1024), 1024)
        self.assertEqual(V.content_length(headers('Content-Length: 0')), 0)

    def test_over_limit(self):
        with self.assertRaises(Invalid):
            V.content_length(headers('Content-Length: 1025'), limit=1024)

    def test_not_a_non_negative_integer(self):
        for raw in ('Content-Length: -1', 'Content-Length: 1.5', 'Content-Length: abc',
                    'Content-Length: 5, 7', 'Content-Length: ١٢٣'):
            with self.subTest(raw=raw), self.assertRaises(Invalid):
                V.content_length(headers(raw))


class JsonBody(unittest.TestCase):
    def test_object(self):
        self.assertEqual(V.json_body(b'{"ym": "2026-03"}'), {'ym': '2026-03'})

    def test_empty_body_is_empty_dict(self):
        for raw in (b'', b'   ', None):
            with self.subTest(raw=raw):
                self.assertEqual(V.json_body(raw), {})

    def test_empty_object_boundary(self):
        self.assertEqual(V.json_body(b'{}'), {})

    def test_broken_json(self):
        with self.assertRaises(Invalid):
            V.json_body(b'{"ym": ')

    def test_json_that_is_not_an_object(self):
        for raw in (b'[1, 2]', b'"2026-03"', b'5', b'null'):
            with self.subTest(raw=raw), self.assertRaises(Invalid):
                V.json_body(raw)


class Text(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(V.text('bus 299 示意站', '路線'), 'bus 299 示意站')

    def test_none_and_empty_use_default(self):
        self.assertEqual(V.text(None, '路線'), '')
        self.assertEqual(V.text('', '路線', default='x'), 'x')

    def test_length_boundary(self):
        self.assertEqual(V.text('a' * 10, '路線', maxlen=10), 'a' * 10)
        with self.assertRaises(Invalid):
            V.text('a' * 11, '路線', maxlen=10)

    def test_wrong_type(self):
        for value in (123, 1.5, ['a'], {'a': 1}, True):
            with self.subTest(value=value), self.assertRaises(Invalid):
                V.text(value, '路線')

    def test_nul_and_pattern(self):
        with self.assertRaises(Invalid):
            V.text('a\x00b', '路線')
        self.assertEqual(V.text('2026', '年', pattern=r'\d{4}'), '2026')
        with self.assertRaises(Invalid):
            V.text('26', '年', pattern=r'\d{4}')


class Date(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(V.date('2026-03-05', '日期'), '2026-03-05')

    def test_range_boundaries(self):
        self.assertEqual(V.date('2000-01-01', '日期'), '2000-01-01')
        self.assertEqual(V.date('2100-12-31', '日期'), '2100-12-31')
        for bad in ('1999-12-31', '2101-01-01'):
            with self.subTest(bad=bad), self.assertRaises(Invalid):
                V.date(bad, '日期')

    def test_bad_format(self):
        for bad in ('', '2026-3-5x', '2026/03/05', '2026-02-30', '今天', '2026-03-05T10:00', 7):
            with self.subTest(bad=bad), self.assertRaises(Invalid):
                V.date(bad, '日期')


class Ym(unittest.TestCase):
    def test_valid_and_boundaries(self):
        for good in ('2026-01', '2026-12', '2000-01', '2099-12'):
            with self.subTest(good=good):
                self.assertEqual(V.ym(good, '年月'), good)

    def test_invalid(self):
        for bad in ('', '2026-00', '2026-13', '2026-1', '26-01', '1999-05', '2026-01-01', 202601, None):
            with self.subTest(bad=bad), self.assertRaises(Invalid):
                V.ym(bad, '年月')


class Number(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(V.number(1234, '金額', 0, 10000), 1234.0)
        self.assertEqual(V.number('1234.5', '金額', 0, 10000), 1234.5)

    def test_range_boundaries(self):
        self.assertEqual(V.number(0, '金額', 0, 24), 0.0)
        self.assertEqual(V.number(24, '金額', 0, 24), 24.0)
        for bad in (-0.5, 24.5):
            with self.subTest(bad=bad), self.assertRaises(Invalid):
                V.number(bad, '金額', 0, 24)

    def test_invalid(self):
        for bad in ('abc', '', None, True, False, [1], {'a': 1}, float('nan'), float('inf'), 'nan', 'inf'):
            with self.subTest(bad=bad), self.assertRaises(Invalid):
                V.number(bad, '金額', 0, 10000)


class Integer(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(V.integer('2026', '年份', 2000, 2100), 2026)
        self.assertEqual(V.integer(2026, '年份', 2000, 2100), 2026)

    def test_empty_uses_default(self):
        self.assertIsNone(V.integer('', '年份', 2000, 2100))
        self.assertIsNone(V.integer(None, '年份', 2000, 2100))
        self.assertEqual(V.integer('', '年份', 2000, 2100, default=2026), 2026)

    def test_range_boundaries(self):
        self.assertEqual(V.integer('2000', '年份', 2000, 2100), 2000)
        self.assertEqual(V.integer('2100', '年份', 2000, 2100), 2100)
        for bad in ('1999', '2101'):
            with self.subTest(bad=bad), self.assertRaises(Invalid):
                V.integer(bad, '年份', 2000, 2100)

    def test_invalid(self):
        for bad in ('20x6', '2026.0', '٢٠٢٦', '9' * 5000, True, 2.5, ['2026']):
            with self.subTest(bad=bad), self.assertRaises(Invalid):
                V.integer(bad, '年份', 2000, 2100)


class PathPart(unittest.TestCase):
    """目前沒有端點收路徑參數，規則先用測試釘住（見 dash_validate.path_part 的說明）。"""

    def test_valid(self):
        for good in ('report_20300101.csv', '示意檔名'):
            with self.subTest(good=good):
                self.assertEqual(V.path_part(good, '檔名'), good)

    def test_length_boundary(self):
        self.assertEqual(V.path_part('a' * 120, '檔名'), 'a' * 120)
        with self.assertRaises(Invalid):
            V.path_part('a' * 121, '檔名')

    def test_rejects_traversal_and_absolute(self):
        for bad in ('..', '../etc/passwd', 'a/../b', 'data/notes.json', '/etc/passwd',
                    '\\windows', 'C:\\x', '.', '', None, 7, 'a\x00b'):
            with self.subTest(bad=bad), self.assertRaises(Invalid):
                V.path_part(bad, '檔名')



class RegressionReview(unittest.TestCase):
    def test_double_minus_is_invalid_not_valueerror_leak(self):
        for bad in ('--5', '---', '-', '+5', '5-', '1e3', '١٢٣', '9' * 19):
            with self.subTest(bad=bad), self.assertRaises(Invalid):
                V.integer(bad, '數字', -10 ** 20, 10 ** 20)
        self.assertEqual(V.integer('-5', '數字', -10, 10), -5)

    def test_json_body_rejects_nonfinite_constants(self):
        for raw in (b'{"a": NaN}', b'{"a": Infinity}', b'{"a": -Infinity}', b'{"a": [NaN]}'):
            with self.subTest(raw=raw), self.assertRaises(Invalid):
                V.json_body(raw)
        self.assertEqual(V.json_body(b'{"a": 1.5}'), {'a': 1.5})


if __name__ == "__main__":
    unittest.main()
