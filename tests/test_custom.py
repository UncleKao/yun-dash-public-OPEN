"""「自訂」分頁：卡片宣告的驗證、假 HA 的各種回應、快取、HTTP 端點。"""
import copy
import datetime
import glob
import json
import os
import tempfile
import threading
import time
import unittest

import helpers
import fake_ha
import dash_cardspec as CS
import dash_config as C
import dash_custom as K
from dash_support import APIError, NotConfigured
from test_server import Running, js

UTC = datetime.timezone.utc
ROOT = helpers.ROOT


def load_cards(cards, **custom):
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 'c.json')
        with open(p, 'w', encoding='utf-8') as f:
            json.dump({'custom': dict({'cards': cards}, **custom)}, f)
        return C.load(p)['custom']['cards']


def vcard(**kw):
    c = {'type': 'value', 'title': '測試', 'entities': ['sensor.a']}
    c.update(kw)
    return c


class SpecAccepts(unittest.TestCase):
    def test_minimal_cards_and_defaults(self):
        cards = load_cards([vcard(), {'type': 'chart', 'title': 'c', 'entity': 'sensor.a'},
                            {'type': 'list', 'title': 'l', 'entities': ['sensor.a', {'id': 'sensor.b', 'name': 'B'}]},
                            {'type': 'list', 'title': 'f', 'filter': {'domain': 'sensor', 'unit': 'W'}}])
        self.assertEqual(cards[0]['entities'][0], {'id': 'sensor.a', 'name': '', 'icon': '', 'unit': '', 'decimals': None})
        self.assertEqual(cards[1]['hours'], 24)
        self.assertEqual(cards[2]['limit'], 30)
        self.assertEqual(cards[3]['filter'], {'domain': 'sensor', 'unit': 'W', 'contains': [], 'exclude': []})

    def test_comment_keys_are_ignored(self):
        cards = load_cards([dict(vcard(), _說明='這是註解', entities=[{'id': 'sensor.a', '_x': 1}])])
        self.assertEqual(len(cards), 1)

    def test_boundaries(self):
        load_cards([{'type': 'chart', 'title': 'x' * 40, 'entity': 'sensor.a', 'hours': 168, 'decimals': 6},
                    {'type': 'chart', 'title': 'x', 'entity': 'sensor.a', 'hours': 1, 'decimals': 0}])
        load_cards([vcard() for _ in range(CS.MAX_CARDS)])
        load_cards([vcard(entities=['sensor.a'] * CS.MAX_VALUE_ENTITIES)])
        load_cards([{'type': 'list', 'title': 'l', 'entities': ['sensor.a'] * CS.MAX_LIST_ENTITIES, 'limit': 50}])
        load_cards([vcard(link='http://homeassistant.local:8123/lovelace/x'), vcard(link='https://example.com/a?b=c')])

    def test_all_shipped_examples_are_valid(self):
        files = glob.glob(os.path.join(ROOT, 'docs', 'examples', '*.json'))
        self.assertGreaterEqual(len(files), 3)
        for f in files:
            with self.subTest(file=os.path.basename(f)):
                self.assertTrue(C.load(f)['custom']['cards'])

    def test_config_example_cards_are_valid(self):
        self.assertEqual(len(C.load(os.path.join(ROOT, 'config.example.json'))['custom']['cards']), 3)


class SpecRejects(unittest.TestCase):
    def err(self, cards, **custom):
        with self.assertRaises(C.ConfigError) as cm:
            load_cards(cards, **custom)
        return str(cm.exception)

    def test_message_names_the_card_and_field(self):
        m = self.err([vcard(), vcard(title='溫度', entities=['Sensor.Bad'])])
        self.assertIn('第 2 張卡', m)
        self.assertIn('溫度', m)
        self.assertIn('entities[1].id', m)
        self.assertIn('開發者工具', m)                     # 告訴使用者去哪查實體 ID

    def test_bad_entity_ids(self):
        for bad in ('sensor', 'Sensor.a', 'sensor.A', 'sensor.a b', 'sensor.', '.a', 'sensor.a.b', 'sensor.' + 'x' * 100, '', 5, None, ['sensor.a'], 'sensor.é'):
            with self.subTest(bad=bad):
                self.assertIn('實體 ID', self.err([vcard(entities=[bad])]))
        self.assertIn('實體 ID', self.err([{'type': 'chart', 'title': 'c', 'entity': 'bad'}]))

    def test_types(self):
        self.assertIn('只能是 value、chart、list', self.err([{'type': 'gauge', 'title': 'x'}]))
        for t in ('button', 'switch', 'service', 'light', 'call_service', 'toggle', 'script'):
            self.assertIn('唯讀', self.err([{'type': t, 'title': 'x', 'entities': ['switch.a']}]))
        self.assertIn('type', self.err([{'title': 'x'}]))
        self.assertIn('是不是想寫「value」', self.err([{'type': 'valeu', 'title': 'x'}]))
        self.assertIn('要是一組設定', self.err(['not a card']))
        self.assertIn('卡片的清單', self.err('oops'))

    def test_unknown_keys_with_hint(self):
        self.assertIn('是不是想寫「entities」', self.err([vcard(entites=['sensor.a'])]))
        self.assertIn('是不是想寫「hours」', self.err([{'type': 'chart', 'title': 'c', 'entity': 'sensor.a', 'hour': 24}]))
        self.assertIn('不認得的欄位', self.err([vcard(service='light.turn_on')]))
        self.assertIn('不認得的欄位', self.err([vcard(entities=[{'id': 'sensor.a', 'color': 'red'}])]))
        self.assertIn('不認得的欄位', self.err([{'type': 'list', 'title': 'l', 'filter': {'domian': 'sensor'}}]))

    def test_limits(self):
        self.assertIn('太多張', self.err([vcard() for _ in range(CS.MAX_CARDS + 1)]))
        self.assertIn('太多', self.err([vcard(entities=['sensor.a'] * (CS.MAX_VALUE_ENTITIES + 1))]))
        self.assertIn('太多', self.err([{'type': 'list', 'title': 'l', 'entities': ['sensor.a'] * (CS.MAX_LIST_ENTITIES + 1)}]))
        for hours in (0, -1, 169, 1000, '24', 2.5, True):
            with self.subTest(hours=hours):
                self.assertIn('hours', self.err([{'type': 'chart', 'title': 'c', 'entity': 'sensor.a', 'hours': hours}]))
        for limit in (0, 51, '5', True):
            self.assertIn('limit', self.err([{'type': 'list', 'title': 'l', 'entities': ['sensor.a'], 'limit': limit}]))
        self.assertIn('decimals', self.err([vcard(entities=[{'id': 'sensor.a', 'decimals': 7}])]))
        self.assertIn('decimals', self.err([{'type': 'chart', 'title': 'c', 'entity': 'sensor.a', 'decimals': -1}]))

    def test_text_fields(self):
        self.assertIn('title', self.err([vcard(title='')]))
        self.assertIn('title', self.err([{'type': 'value', 'entities': ['sensor.a']}]))
        self.assertIn('title', self.err([vcard(title='x' * 41)]))
        self.assertIn('控制字元', self.err([vcard(title='a\x1bb')]))
        self.assertIn('文字', self.err([vcard(title=5)]))
        self.assertIn('note', self.err([vcard(note='x' * 201)]))
        self.assertIn('icon', self.err([vcard(icon='x' * 17)]))
        self.assertIn('name', self.err([vcard(entities=[{'id': 'sensor.a', 'name': 'x' * 41}])]))

    def test_links(self):
        for bad in ('javascript:alert(1)', 'data:text/html,hi', 'ftp://x.example', '//example.com', 'example.com', 'file:///etc/passwd', 'http://', 'https:///x', 5):
            with self.subTest(bad=bad):
                self.assertIn('link', self.err([vcard(link=bad)]))

    def test_list_card_rules(self):
        self.assertIn('擇一', self.err([{'type': 'list', 'title': 'l'}]))
        self.assertIn('擇一', self.err([{'type': 'list', 'title': 'l', 'entities': ['sensor.a'], 'filter': {'domain': 'sensor'}}]))
        self.assertIn('至少要填', self.err([{'type': 'list', 'title': 'l', 'filter': {}}]))
        self.assertIn('至少要填', self.err([{'type': 'list', 'title': 'l', 'filter': {'exclude': ['x']}}]))
        self.assertIn('sort', self.err([{'type': 'list', 'title': 'l', 'entities': ['sensor.a'], 'sort': 'random'}]))
        self.assertIn('filter.contains', self.err([{'type': 'list', 'title': 'l', 'filter': {'contains': 'power'}}]))
        self.assertIn('filter', self.err([{'type': 'list', 'title': 'l', 'filter': 'sensor'}]))
        self.assertIn('entity', self.err([{'type': 'chart', 'title': 'c'}]))
        self.assertIn('entities', self.err([{'type': 'value', 'title': 'v'}]))
        self.assertIn('entities', self.err([vcard(entities=[])]))

    def test_custom_section_values(self):
        for bad in ({'cache_seconds': 5}, {'cache_seconds': 5000}, {'title': ''}, {'title': 'x' * 13}, {'title': 5}):
            with self.subTest(bad=bad), self.assertRaises(C.ConfigError):
                load_cards([vcard()], **bad)
        with self.assertRaises(C.ConfigError):
            with tempfile.TemporaryDirectory() as d:
                p = os.path.join(d, 'c.json')
                with open(p, 'w') as f:
                    json.dump({'ha': {'timeout_seconds': 1}}, f)
                C.load(p)

    def test_startup_prints_chinese_message_without_traceback(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'c.json')
            with open(p, 'w', encoding='utf-8') as f:
                json.dump({'custom': {'cards': [vcard(), {'type': 'chart', 'title': '溫度走勢', 'entity': 'sensor.a', 'hours': 999}]}}, f)
            r = subprocess.run([sys.executable, 'server.py', '--config', p, '--port', '0'], cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('設定檔有問題', r.stderr)
        self.assertIn('第 2 張卡', r.stderr)
        self.assertIn('溫度走勢', r.stderr)
        self.assertIn('hours', r.stderr)
        self.assertNotIn('Traceback', r.stderr)


def st(state, unit=None, name=None, age=60, extra=None):
    now = datetime.datetime.now(UTC)
    attrs = {}
    if unit is not None:
        attrs['unit_of_measurement'] = unit
    if name is not None:
        attrs['friendly_name'] = name
    attrs.update(extra or {})
    return {'state': state, 'attributes': attrs, 'last_changed': fake_ha.iso(now - datetime.timedelta(seconds=age)), 'last_updated': fake_ha.iso(now)}


def hist_rows(values, hours=24):
    now = datetime.datetime.now(UTC)
    n = len(values)
    return [(fake_ha.iso(now - datetime.timedelta(hours=hours) + datetime.timedelta(hours=hours * i / max(n, 1))), str(v)) for i, v in enumerate(values)]


class Env:
    """with Env(states, hist, cards) as (ha, call?)：假 HA ＋ 設定。"""

    def __init__(self, states=None, hist=None, cards=None, raw=None, **extra):
        self.ha = fake_ha.FakeHA(states or {}, hist or {}, raw=raw)
        cfg = {'ha': {'url': self.ha.url, 'token_env': 'DASH_FAKE_HA_TOKEN'}, 'custom': {'cards': cards or []}}
        cfg.update(extra)
        self.cm = helpers.config(cfg)

    def __enter__(self):
        os.environ['DASH_FAKE_HA_TOKEN'] = fake_ha.TOKEN
        self.cm.__enter__()
        return self.ha

    def __exit__(self, *a):
        self.cm.__exit__(*a)
        os.environ.pop('DASH_FAKE_HA_TOKEN', None)
        self.ha.close()


class ValueCards(unittest.TestCase):
    def test_values_units_names_and_formatting(self):
        states = {'sensor.t': st('21.456', '°C', '客廳溫度'), 'sensor.h': st('48', '%'), 'sensor.off': st('unavailable', 'W', '斷線'),
                  'sensor.txt': st('closed', None, '門'), 'sensor.big': st('1234567.891', 'Wh')}
        cards = [{'type': 'value', 'title': 'v', 'entities': ['sensor.t', {'id': 'sensor.h', 'name': '濕度', 'decimals': 0, 'icon': '💧'}, 'sensor.off',
                                                               'sensor.txt', 'sensor.missing', {'id': 'sensor.big', 'unit': 'kWh', 'decimals': 1}]}]
        with Env(states, cards=cards):
            d = K.render()
        e = {x['id']: x for x in d['cards'][0]['entities']}
        self.assertEqual((e['sensor.t']['display'], e['sensor.t']['unit'], e['sensor.t']['name']), ('21.46', '°C', '客廳溫度'))
        self.assertEqual((e['sensor.h']['display'], e['sensor.h']['name'], e['sensor.h']['icon']), ('48', '濕度', '💧'))
        self.assertFalse(e['sensor.off']['available'])
        self.assertEqual(e['sensor.off']['display'], '無法使用')
        self.assertIsNone(e['sensor.off']['value'])
        self.assertEqual((e['sensor.txt']['display'], e['sensor.txt']['value']), ('closed', None))
        self.assertFalse(e['sensor.missing']['found'])
        self.assertEqual(e['sensor.missing']['display'], '找不到')
        self.assertEqual((e['sensor.big']['display'], e['sensor.big']['unit']), ('1234567.9', 'kWh'))     # 設定的單位蓋過 HA 的
        self.assertTrue(e['sensor.t']['changed'])

    def test_header_fields(self):
        with Env({'sensor.a': st('1')}, cards=[vcard(title='標題', icon='🔥', note='說明', link='https://example.com/x')]):
            d = K.render()
        c = d['cards'][0]
        self.assertEqual((c['title'], c['icon'], c['note'], c['link'], c['type']), ('標題', '🔥', '說明', 'https://example.com/x', 'value'))
        self.assertEqual(d['title'], '自訂')


class ListCards(unittest.TestCase):
    STATES = {'sensor.plug_a_power': st('50.5', 'W', 'Plug A'), 'sensor.plug_b_power': st('120', 'W', 'Plug B'), 'sensor.plug_c_power': st('0', 'W', 'Plug C'),
              'sensor.ups_nominal_power': st('1000', 'W', '額定'), 'sensor.temp': st('22', '°C', 'Temp'), 'light.lamp': st('on', None, 'Lamp'),
              'sensor.broken_power': st('unavailable', 'W', 'Broken')}

    def render(self, card):
        with Env(self.STATES, cards=[card]):
            return K.render()['cards'][0]

    def test_filter_domain_unit_sort_and_total(self):
        c = self.render({'type': 'list', 'title': 'l', 'filter': {'domain': 'sensor', 'unit': 'W'}})
        names = [e['name'] for e in c['entities']]
        self.assertEqual(names, ['額定', 'Plug B', 'Plug A', 'Plug C', 'Broken'])          # 預設由大到小，無法使用的排最後
        self.assertEqual(c['total'], 5)

    def test_contains_is_and_exclude_is_any_case_insensitive(self):
        c = self.render({'type': 'list', 'title': 'l', 'filter': {'contains': ['PLUG', 'power'], 'exclude': ['plug_c']}})
        self.assertEqual({e['id'] for e in c['entities']}, {'sensor.plug_a_power', 'sensor.plug_b_power'})
        c = self.render({'type': 'list', 'title': 'l', 'filter': {'domain': 'sensor', 'exclude': ['nominal', '額定', 'temp', 'broken']}})
        self.assertEqual(len(c['entities']), 3)
        self.assertEqual(self.render({'type': 'list', 'title': 'l', 'filter': {'contains': ['nothing-matches']}})['entities'], [])

    def test_sort_options_and_limit(self):
        c = self.render({'type': 'list', 'title': 'l', 'filter': {'unit': 'W'}, 'sort': 'value_asc', 'limit': 2})
        self.assertEqual([e['name'] for e in c['entities']], ['Plug C', 'Plug A'])
        self.assertEqual((c['total'], c['limit']), (5, 2))
        c = self.render({'type': 'list', 'title': 'l', 'filter': {'domain': 'light'}, 'sort': 'name'})
        self.assertEqual([e['id'] for e in c['entities']], ['light.lamp'])

    def test_explicit_entities_keep_order_unless_sorted(self):
        ents = ['sensor.plug_c_power', 'sensor.plug_b_power', 'sensor.nope']
        c = self.render({'type': 'list', 'title': 'l', 'entities': ents})
        self.assertEqual([e['id'] for e in c['entities']], ents)
        self.assertFalse(c['entities'][2]['found'])
        c = self.render({'type': 'list', 'title': 'l', 'entities': ents, 'sort': 'value_desc'})
        self.assertEqual(c['entities'][0]['id'], 'sensor.plug_b_power')


class ChartCards(unittest.TestCase):
    def test_points_min_max_and_unit(self):
        vals = [20, 21, 22.5, 19, 'unavailable', 'abc', 23]
        with Env({'sensor.t': st('23', '°C', 'Temp')}, {'sensor.t': hist_rows(vals)}, [{'type': 'chart', 'title': 'c', 'entity': 'sensor.t'}]) as ha:
            c = K.render()['cards'][0]
        ch = c['chart']
        self.assertEqual(c['hours'], 24)
        self.assertEqual(ch['count'], 5)                             # 非數字的略過
        self.assertEqual((ch['min'], ch['max']), (19, 23))
        self.assertEqual(c['entity']['unit'], '°C')
        self.assertEqual(c['entity']['name'], 'Temp')
        self.assertEqual([p[1] for p in ch['points']], [20, 21, 22.5, 19, 23])
        self.assertTrue(all(ch['start_ms'] <= p[0] <= ch['end_ms'] for p in ch['points']))
        self.assertEqual([p[0] for p in ch['points']], sorted(p[0] for p in ch['points']))

    def test_hours_is_sent_to_ha_and_capped_by_config(self):
        with Env({'sensor.t': st('1')}, {'sensor.t': hist_rows([1, 2])}, [{'type': 'chart', 'title': 'c', 'entity': 'sensor.t', 'hours': 168}]) as ha:
            K.render()
            hq = [r for r in ha.requests if '/api/history/period/' in r][0]
            import urllib.parse
            start = urllib.parse.unquote(hq.split('/api/history/period/')[1].split('?')[0])
            age = datetime.datetime.now(UTC) - datetime.datetime.fromisoformat(start)
            self.assertAlmostEqual(age.total_seconds() / 3600, 168, delta=0.1)

    def test_downsampling_keeps_shape_and_limit(self):
        vals = [i % 50 for i in range(1000)]
        with Env({'sensor.t': st('1')}, {'sensor.t': hist_rows(vals)}, [{'type': 'chart', 'title': 'c', 'entity': 'sensor.t'}]):
            ch = K.render()['cards'][0]['chart']
        self.assertEqual(ch['count'], 1000)
        self.assertLessEqual(len(ch['points']), K.MAX_POINTS)
        self.assertGreater(len(ch['points']), 100)
        self.assertLessEqual(ch['max'], 49)

    def test_missing_entity_marks_only_that_card(self):
        cards = [{'type': 'chart', 'title': 'c', 'entity': 'sensor.missing'}, vcard(entities=['sensor.a'])]
        with Env({'sensor.a': st('1')}, cards=cards):
            d = K.render()
        self.assertIn('找不到這個實體', d['cards'][0]['error'])
        self.assertNotIn('error', d['cards'][1])

    def test_empty_history_is_not_an_error(self):
        with Env({'sensor.t': st('1')}, {}, [{'type': 'chart', 'title': 'c', 'entity': 'sensor.t'}]):
            c = K.render()['cards'][0]
        self.assertEqual(c['chart']['points'], [])
        self.assertEqual(c['chart']['count'], 0)
        self.assertIsNone(c['chart']['min'])


class DefensiveHa(unittest.TestCase):
    def test_malformed_states_are_skipped_or_marked(self):
        now = datetime.datetime.now(UTC)
        weird = [{'entity_id': 'sensor.a', 'state': '5', 'attributes': {'unit_of_measurement': 'W', 'friendly_name': '甲'}, 'last_changed': fake_ha.iso(now)},
                 {'entity_id': 'sensor.b', 'attributes': {'unit_of_measurement': 'W'}},                                      # 缺 state、last_changed
                 {'entity_id': 'sensor.c', 'state': '5', 'last_changed': 'not-a-date', 'attributes': {'unit_of_measurement': 'W'}},
                 {'entity_id': 'sensor.d', 'state': 5, 'last_changed': 12345, 'attributes': 'oops'},                          # state 不是字串、attributes 不是物件
                 {'entity_id': 'sensor.e', 'state': None, 'attributes': {'unit_of_measurement': 7, 'friendly_name': ['x']}},
                 {'state': '1'}, 'garbage', 42, None, {'entity_id': 5}]
        cards = [vcard(entities=['sensor.a', 'sensor.b', 'sensor.c', 'sensor.d', 'sensor.e', 'sensor.zzz']),
                 {'type': 'list', 'title': 'l', 'filter': {'domain': 'sensor'}}]
        with Env(raw={'/api/states': weird}, cards=cards):
            d = K.render()
        e = {x['id']: x for x in d['cards'][0]['entities']}
        self.assertEqual(e['sensor.a']['display'], '5')
        self.assertEqual(e['sensor.d']['display'], '5')
        self.assertEqual(e['sensor.e']['unit'], '')
        self.assertFalse(e['sensor.zzz']['found'])
        self.assertEqual(len(d['cards'][1]['entities']), 5)

    def test_malformed_history_rows_are_skipped(self):
        now = datetime.datetime.now(UTC)
        hist = [[{'entity_id': 'sensor.t', 'state': '10', 'last_changed': fake_ha.iso(now - datetime.timedelta(hours=1))},
                 {'state': '11'}, {'last_changed': fake_ha.iso(now)}, 'x', None, {'state': 'inf', 'last_changed': fake_ha.iso(now)},
                 {'state': 'nan', 'last_changed': fake_ha.iso(now)}, {'state': '12', 'last_changed': 'bad'},
                 {'state': '13', 'last_changed': fake_ha.iso(now)}], [], None, 'oops', [None], [{'no': 'entity_id'}]]
        with Env({'sensor.t': st('1')}, cards=[{'type': 'chart', 'title': 'c', 'entity': 'sensor.t'}],
                 raw={'/api/history/period/': hist}):
            c = K.render()['cards'][0]
        self.assertEqual([p[1] for p in c['chart']['points']], [10.0, 13.0])

    def test_wrong_top_level_shapes_are_502(self):
        for body in ({'not': 'a list'}, 'text', 5):
            with Env(cards=[vcard()], raw={'/api/states': body}):
                with self.assertRaises(APIError) as cm:
                    K.render()
                self.assertEqual((cm.exception.status, cm.exception.code), (502, 'ha_shape'))
        with Env({'sensor.t': st('1')}, cards=[{'type': 'chart', 'title': 'c', 'entity': 'sensor.t'}], raw={'/api/history/period/': {'oops': 1}}):
            d = K.render()                                      # 只有圖表失敗：標在那張卡上，不影響其他
            self.assertIn('格式', d['cards'][0]['error'])
        with Env(cards=[{'type': 'chart', 'title': 'c', 'entity': 'sensor.t'}], raw={'/api/states/sensor.t': ['a', 'b']}):
            self.assertIn('格式', K.render()['cards'][0]['error'])

    def test_text_is_truncated(self):
        with Env({'sensor.a': st('x' * 5000, 'u' * 500, 'n' * 500)}, cards=[vcard()]):
            e = K.render()['cards'][0]['entities'][0]
        self.assertLessEqual(len(e['state']), 200)
        self.assertLessEqual(len(e['name']), 80)
        self.assertLessEqual(len(e['unit']), 12)
        self.assertLessEqual(len(e['display']), 60)

    def test_html_in_names_and_states_is_returned_as_plain_text_data(self):
        payload = '<img src=x onerror=alert(1)>'
        with Env({'sensor.a': st(payload, payload, payload)}, cards=[vcard(title='<b>t</b>')]):
            c = K.render()['cards'][0]
        self.assertEqual(c['title'], '<b>t</b>')                # 後端不改寫；前端一律用 textContent（見 tests/app.test.mjs）
        self.assertIn('<img', c['entities'][0]['name'])


class Failures(unittest.TestCase):
    def test_timeout_is_504_with_reason(self):
        with Env({'sensor.a': st('1')}, cards=[vcard()]) as ha:
            C.CFG['ha']['timeout_seconds'] = 0.5
            ha.delay = 1.5
            with self.assertRaises(APIError) as cm:
                K.render()
            self.assertEqual((cm.exception.status, cm.exception.code), (504, 'ha_timeout'))
            self.assertIn('逾時', str(cm.exception))

    def test_chart_timeout_only_marks_that_card(self):
        with Env({'sensor.a': st('1')}, cards=[vcard(), {'type': 'chart', 'title': 'c', 'entity': 'sensor.a'}]) as ha:
            K.render()                                          # 先把 states 快取起來
            K._cache.data.pop(('state', 'sensor.a'), None)
            C.CFG['ha']['timeout_seconds'] = 0.5
            ha.delay = 1.5
            d = K.render()
        self.assertNotIn('error', d['cards'][0])
        self.assertIn('逾時', d['cards'][1]['error'])

    def test_unreachable_auth_and_redirect(self):
        os.environ['DASH_FAKE_HA_TOKEN'] = fake_ha.TOKEN
        try:
            with helpers.config({'ha': {'url': 'http://127.0.0.1:1', 'token_env': 'DASH_FAKE_HA_TOKEN'}, 'custom': {'cards': [vcard()]}}):
                with self.assertRaises(APIError) as cm:
                    K.render()
                self.assertEqual(cm.exception.code, 'ha_unreachable')
        finally:
            os.environ.pop('DASH_FAKE_HA_TOKEN', None)
        with Env({'sensor.a': st('1')}, cards=[vcard()]):
            os.environ['DASH_FAKE_HA_TOKEN'] = 'wrong'
            with self.assertRaises(APIError) as cm:
                K.render()
            self.assertEqual(cm.exception.code, 'ha_auth')

    def test_failures_are_not_cached(self):
        with Env({'sensor.a': st('1')}, cards=[vcard()]) as ha:
            os.environ['DASH_FAKE_HA_TOKEN'] = 'wrong'
            with self.assertRaises(APIError):
                K.render()
            os.environ['DASH_FAKE_HA_TOKEN'] = fake_ha.TOKEN
            self.assertEqual(K.render()['cards'][0]['entities'][0]['display'], '1')


class NotConfiguredStates(unittest.TestCase):
    def test_reasons_point_to_the_manual(self):
        with helpers.config():
            with self.assertRaises(NotConfigured) as cm:
                K.render()
            self.assertIn('custom.cards', cm.exception.reason)
            self.assertIn('自訂分頁說明書', cm.exception.reason)
        with helpers.config({'custom': {'cards': [vcard()]}}):
            with self.assertRaises(NotConfigured) as cm:
                K.render()
            self.assertIn('ha.url', cm.exception.reason)
            self.assertIn('自訂分頁說明書', cm.exception.reason)
        with helpers.config({'ha': {'url': 'http://ha.invalid', 'token_env': 'DASH_NO_SUCH_ENV'}, 'custom': {'cards': [vcard()]}}):
            with self.assertRaises(NotConfigured) as cm:
                K.render()
            self.assertIn('權杖', cm.exception.reason)

    def test_loose_token_file(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, 'tok')
            with open(f, 'w') as fh:
                fh.write('t')
            os.chmod(f, 0o644)
            with helpers.config({'ha': {'url': 'http://ha.invalid', 'token_env': 'DASH_NO_SUCH_ENV', 'token_file': f}, 'custom': {'cards': [vcard()]}}):
                with self.assertRaises(NotConfigured) as cm:
                    K.render()
                self.assertIn('chmod 600', cm.exception.reason)


class Caching(unittest.TestCase):
    def count(self, ha, prefix):
        return sum(1 for r in ha.requests if r.startswith(prefix))

    def test_many_renders_hit_ha_once_and_expiry_refetches(self):
        cards = [vcard(), {'type': 'list', 'title': 'l', 'filter': {'domain': 'sensor'}}, {'type': 'chart', 'title': 'c', 'entity': 'sensor.a'}]
        with Env({'sensor.a': st('1')}, {'sensor.a': hist_rows([1, 2, 3])}, cards) as ha:
            clock = [1000.0]
            K._cache.clock = lambda: clock[0]
            try:
                for _ in range(6):
                    K.render()
                self.assertEqual(self.count(ha, '/api/states'), 2)       # 一次全部狀態＋圖表的單一實體狀態
                self.assertEqual(self.count(ha, '/api/states/sensor.a'), 1)
                self.assertEqual(self.count(ha, '/api/history/period/'), 1)
                clock[0] += 31
                K.render()
                self.assertEqual(self.count(ha, '/api/history/period/'), 2)
                self.assertEqual(self.count(ha, '/api/states/sensor.a'), 2)
            finally:
                K._cache.clock = time.monotonic

    def test_cache_seconds_setting_is_respected(self):
        with Env({'sensor.a': st('1')}, cards=[vcard()]) as ha:
            C.CFG['custom']['cache_seconds'] = 600
            clock = [0.0]
            K._cache.clock = lambda: clock[0]
            try:
                K.render()
                clock[0] += 500
                K.render()
                self.assertEqual(self.count(ha, '/api/states'), 1)
                clock[0] += 200
                K.render()
                self.assertEqual(self.count(ha, '/api/states'), 2)
            finally:
                K._cache.clock = time.monotonic

    def test_value_only_cards_do_not_fetch_history_and_chart_only_does_not_fetch_all_states(self):
        with Env({'sensor.a': st('1')}, cards=[vcard()]) as ha:
            K.render()
            self.assertEqual(self.count(ha, '/api/history/'), 0)
        with Env({'sensor.a': st('1')}, {'sensor.a': hist_rows([1, 2])}, [{'type': 'chart', 'title': 'c', 'entity': 'sensor.a'}]) as ha:
            K.render()
            self.assertEqual([r for r in ha.requests if r.split('?')[0] == '/api/states'], [])

    def test_concurrent_requests_share_one_fetch(self):
        with Env({'sensor.a': st('1')}, cards=[vcard()]) as ha:
            ha.delay = 0.3
            ts = [threading.Thread(target=K.render) for _ in range(8)]
            [t.start() for t in ts]
            [t.join(10) for t in ts]
            self.assertEqual(self.count(ha, '/api/states'), 1)


class ThroughHttp(unittest.TestCase):
    def test_not_configured_and_features(self):
        with Running() as call:
            d = js(call('GET', '/api/custom')[2])
            self.assertFalse(d['configured'])
            self.assertIn('自訂分頁說明書', d['reason'])
            f = js(call('GET', '/api/features')[2])
            self.assertFalse(f['features']['custom']['ready'])
            self.assertEqual(f['custom_title'], '自訂')

    def test_configured_flow(self):
        fh = fake_ha.FakeHA({'sensor.a': st('7.5', 'W', 'A')}, {'sensor.a': hist_rows([1, 2, 3])})
        os.environ['DASH_FAKE_HA_TOKEN'] = fake_ha.TOKEN
        try:
            cards = [vcard(), {'type': 'chart', 'title': 'c', 'entity': 'sensor.a'}, {'type': 'list', 'title': 'l', 'filter': {'unit': 'W'}}]
            with Running({'ha': {'url': fh.url, 'token_env': 'DASH_FAKE_HA_TOKEN'}, 'custom': {'cards': cards, 'title': '健康'}}) as call:
                f = js(call('GET', '/api/features')[2])
                self.assertTrue(f['features']['custom']['ready'])
                self.assertEqual(f['custom_title'], '健康')
                st_, _, raw = call('GET', '/api/custom')
                self.assertEqual(st_, 200)
                d = js(raw)
                self.assertEqual([c['type'] for c in d['cards']], ['value', 'chart', 'list'])
                self.assertEqual(d['title'], '健康')
        finally:
            os.environ.pop('DASH_FAKE_HA_TOKEN', None)
            fh.close()

    def test_ha_errors_are_json_and_feature_can_be_disabled(self):
        os.environ['DASH_FAKE_HA_TOKEN'] = 'x'
        try:
            with Running({'ha': {'url': 'http://127.0.0.1:1', 'token_env': 'DASH_FAKE_HA_TOKEN'}, 'custom': {'cards': [vcard()]}}) as call:
                st_, _, raw = call('GET', '/api/custom')
                self.assertEqual(st_, 502)
                self.assertIn('Home Assistant', js(raw)['error'])
        finally:
            os.environ.pop('DASH_FAKE_HA_TOKEN', None)
        with Running({'features': {'custom': False}}) as call:
            self.assertEqual(call('GET', '/api/custom')[0], 404)

    def test_no_write_endpoints_exist(self):
        import server
        self.assertEqual(sorted(server.POST_ROUTES), ['/api/food/tried'])          # 沒有任何寫入／控制 HA 的端點
        with open(os.path.join(ROOT, 'dash_ha.py'), encoding='utf-8') as f:
            src = f.read()
        self.assertNotIn("method='POST'", src)
        self.assertNotIn('/api/services', src)


if __name__ == '__main__':
    unittest.main()
