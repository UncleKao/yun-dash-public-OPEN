"""「自訂」分頁：依設定檔宣告的唯讀卡片，從你的 Home Assistant 讀資料。

卡片格式見 dash_cardspec.py 與說明書 docs/自訂分頁說明書.md。這裡只讀（GET），沒有任何寫入或控制 HA 的功能。
HA 回傳的欄位一律不信任：缺欄位、型別不對的項目會被略過或標成「找不到／無法使用」，不會讓伺服器出錯；
所有文字都有長度上限（前端另外做 HTML 跳脫）。伺服器端快取 custom.cache_seconds 秒，不論有幾個瀏覽器在看，HA 只被問一次。
"""
import datetime
import math

import dash_config as C
import dash_geo as G
import dash_ha
from dash_support import APIError, NotConfigured, TimedCache

MAX_POINTS = 200          # 折線圖最多送給瀏覽器幾個點（超過就依時間分組平均）
UNAVAILABLE = ('unavailable', 'unknown', 'none', '')
NOT_FOUND_MSG = '在 Home Assistant 找不到這個實體（請確認實體 ID 拼對了；到「開發者工具 › 狀態」查）'

_cache = TimedCache(limit=64)


def reset():
    """測試用：清快取。"""
    _cache.data.clear()


def cards():
    return C.CFG['custom']['cards']


def configured_reason():
    """給功能狀態用：還沒好的原因（回空字串＝好了）。不連網。"""
    if not cards():
        return '尚未在設定檔 custom.cards 宣告任何卡片（寫法見說明書 docs/自訂分頁說明書.md）'
    if not C.CFG['ha'].get('url'):
        return '「自訂」分頁的資料來自 Home Assistant，但尚未設定 ha.url 與權杖（見說明書 docs/自訂分頁說明書.md）'
    bad = dash_ha.token_problem()
    if bad:
        return bad
    if not dash_ha.configured():
        return '尚未設定 Home Assistant 權杖（環境變數 HA_TOKEN 或 ha.token_file；見說明書 docs/自訂分頁說明書.md）'
    return ''


def _ttl():
    return C.CFG['custom']['cache_seconds']


def _short(v, n):
    return v[:n] if isinstance(v, str) else ''


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _fmt(x, decimals):
    if decimals is None:
        return f'{x:.2f}'.rstrip('0').rstrip('.') if abs(x) < 1e12 else f'{x:.3g}'
    return f'{x:.{decimals}f}'


def entity_view(item, st):
    """item：卡片裡的實體設定（id、name、icon、unit、decimals）；st：HA 的狀態物件或 None。"""
    attrs = st.get('attributes') if isinstance(st, dict) and isinstance(st.get('attributes'), dict) else {}
    out = {'id': item['id'], 'icon': item.get('icon') or '', 'found': isinstance(st, dict)}
    out['name'] = item.get('name') or _short(attrs.get('friendly_name'), 80) or item['id']
    out['unit'] = item.get('unit') or _short(attrs.get('unit_of_measurement'), 12)
    raw = st.get('state') if isinstance(st, dict) else None
    text = raw if isinstance(raw, str) else ('' if raw is None else str(raw))
    out['state'] = text[:200]
    out['available'] = out['found'] and text.strip().lower() not in UNAVAILABLE
    x = _num(text) if out['available'] else None
    out['value'] = x
    if not out['found']:
        out['display'] = '找不到'
    elif not out['available']:
        out['display'] = '無法使用' if text.strip().lower() in ('unavailable', '') else text[:40] or '—'
    else:
        out['display'] = _fmt(x, item.get('decimals')) if x is not None else text[:60]
    changed = dash_ha.safe_ts(st.get('last_changed')) if isinstance(st, dict) else None
    out['changed'] = changed.astimezone(G.tz()).strftime('%m-%d %H:%M') if changed else ''
    return out


def _states():
    return _cache.get('states', _ttl(), lambda: {s['entity_id']: s for s in dash_ha.states()})


def _match(st, f):
    eid = st['entity_id']
    attrs = st.get('attributes') if isinstance(st.get('attributes'), dict) else {}
    if f['domain'] and eid.split('.', 1)[0] != f['domain']:
        return False
    if f['unit'] and attrs.get('unit_of_measurement') != f['unit']:
        return False
    label = (eid + ' ' + _short(attrs.get('friendly_name'), 80)).lower()
    if f['contains'] and not all(k in label for k in f['contains']):
        return False
    return not any(k in label for k in f['exclude'])


def _list_card(card, states):
    if 'entities' in card:
        rows = [entity_view(e, states.get(e['id'])) for e in card['entities']]
        total = len(rows)
    else:
        f = card['filter']
        rows = [entity_view({'id': s['entity_id']}, s) for s in states.values() if _match(s, f)]
        total = len(rows)
        numeric = any(r['value'] is not None for r in rows)
        sort = card['sort'] or ('value_desc' if numeric else 'name')
        if sort == 'name':
            rows.sort(key=lambda r: (r['name'].lower(), r['id']))
        else:
            rows.sort(key=lambda r: (r['value'] is None, -(r['value'] or 0) if sort == 'value_desc' else (r['value'] or 0), r['id']))
    if 'entities' in card and card['sort']:
        key = card['sort']
        rows.sort(key=lambda r: (r['name'].lower(), r['id']) if key == 'name' else
                  (r['value'] is None, -(r['value'] or 0) if key == 'value_desc' else (r['value'] or 0), r['id']))
    return {'entities': rows[:card['limit']], 'total': total, 'limit': card['limit']}


def _downsample(pts, start_ms, end_ms):
    """pts：[(毫秒, 數值)]，依時間排序。超過 MAX_POINTS 時只為了「畫圖」縮減點數：
    每個時間段保留最低與最高那兩點（所以尖峰與谷底不會被平均掉），並一定保留最前與最後一點的原值。
    最低／最高／最新的統計數字一律用原始資料算（見 _chart_card），不受縮點影響。"""
    if len(pts) <= MAX_POINTS:
        return pts
    n = MAX_POINTS // 2 - 2
    width = max((end_ms - start_ms) / n, 1)
    groups = {}
    for p in pts:
        groups.setdefault(int((p[0] - start_ms) // width), []).append(p)
    keep = {pts[0], pts[-1]}
    for g in groups.values():
        keep.add(min(g, key=lambda p: p[1]))
        keep.add(max(g, key=lambda p: p[1]))
    return sorted(keep)


def _chart_card(card):
    eid, hours = card['entity'], card['hours']
    try:
        st = _cache.get(('state', eid), _ttl(), lambda: dash_ha.state(eid))
    except APIError as e:
        if e.code == 'ha_not_found':
            raise APIError(NOT_FOUND_MSG, 404, 'entity_not_found') from None
        raise
    view = entity_view({'id': eid, 'name': card['name'], 'unit': card['unit'], 'decimals': card['decimals']}, st)

    def build():
        now = datetime.datetime.now(datetime.timezone.utc)
        start = now - datetime.timedelta(hours=hours)
        series = dash_ha.history([eid], start, now).get(eid, [])
        s_ms, e_ms = start.timestamp() * 1000, now.timestamp() * 1000
        pts = []
        for row in series:
            x = _num(row.get('state'))
            ts = dash_ha.safe_ts(row.get('last_changed'))
            if x is None or ts is None:
                continue
            pts.append((max(ts.timestamp() * 1000, s_ms), x))
        pts.sort()
        n = len(pts)
        lo = min((v for _, v in pts), default=None)         # 統計數字用原始資料算，縮點只影響畫面上的點
        hi = max((v for _, v in pts), default=None)
        last = pts[-1][1] if pts else None
        drawn = _downsample(pts, s_ms, e_ms)
        return {'points': [[round(t), v] for t, v in drawn], 'count': n, 'start_ms': round(s_ms), 'end_ms': round(e_ms),
                'min': lo, 'max': hi, 'last': last}
    data = _cache.get(('chart', eid, hours), _ttl(), build)
    return {'entity': view, 'hours': hours, 'decimals': card['decimals'], 'chart': data}


def render():
    """整個分頁的資料：{'title', 'cards': [...], 'fetched'}。HA 讀不到（含沒設定）時丟 NotConfigured／APIError；單張圖表失敗只標在那張卡。"""
    reason = configured_reason()
    if reason:
        raise NotConfigured(reason)
    needs_states = any(c['type'] in ('value', 'list') for c in cards())
    states = _states() if needs_states else {}
    out = []
    for c in cards():
        base = {k: c[k] for k in ('type', 'title', 'icon', 'note', 'link')}
        try:
            if c['type'] == 'value':
                base['entities'] = [entity_view(e, states.get(e['id'])) for e in c['entities']]
            elif c['type'] == 'list':
                base.update(_list_card(c, states))
            else:
                base.update(_chart_card(c))
        except APIError as e:
            base['error'] = str(e)
        out.append(base)
    return {'title': C.CFG['custom']['title'], 'cards': out, 'fetched': G.now().strftime('%H:%M:%S'), 'cache_seconds': _ttl()}
