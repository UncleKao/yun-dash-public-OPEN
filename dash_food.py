"""美食庫：骰子（隨機選一家）、附近的店、分類瀏覽、吃過紀錄。

資料放在 data/food.sqlite 的 places 表（欄位見 SCHEMA）。用 tools/food_import.py 從 CSV 匯入，
範例檔見 data.example/places.sample.csv。完全離線，不呼叫任何外部服務。
"""
import datetime
import math
import os
import random
import re
import sqlite3
import time

import dash_commute
import dash_config as C
import dash_geo as G
from dash_support import APIError, NotConfigured, store_lock
import dash_validate as V

SCHEMA = """
CREATE TABLE IF NOT EXISTS places (
  id        INTEGER PRIMARY KEY,
  name      TEXT NOT NULL,
  category  TEXT,
  address   TEXT,
  district  TEXT,
  lat       REAL,
  lng       REAL,
  price     INTEGER,        -- 一人大約花費（元）
  rating    REAL,
  reviews   INTEGER,
  hours     TEXT,           -- 營業時間，格式見 parse_hours
  note      TEXT,
  tried_at  TEXT,           -- 吃過的日期 YYYY-MM-DD
  my_rating REAL,           -- 自己的評分
  excluded  INTEGER NOT NULL DEFAULT 0,   -- 1＝不要出現（歇業、不喜歡…）
  UNIQUE(name, address)
);
CREATE INDEX IF NOT EXISTS idx_places_geo ON places(lat, lng);
"""

TIER_NAME = {'daily': '平價', 'mid': '吃好一點', 'high': '高價'}
DAYS = '一二三四五六日'


def db_path():
    return C.data_path(C.CFG['food']['db'])


def require():
    if not os.path.exists(db_path()):
        raise NotConfigured('還沒有美食庫：請先用 tools/food_import.py 匯入店家 CSV（範例見 data.example/places.sample.csv）')


def connect(write=False):
    require()
    if write:
        return sqlite3.connect(db_path())
    return sqlite3.connect(f'file:{db_path()}?mode=ro', uri=True)


def init_db(path=None):
    path = path or db_path()
    C.ensure_data_dir()
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.commit()
    os.chmod(path, 0o600)
    return db


# ── 營業時間 ──
def _day_set(spec):
    spec = spec.replace('週', '').replace('周', '').replace('星期', '').strip()
    if spec in ('每天', '天天', '全週', '全周'):
        return set(range(7))
    m = re.fullmatch(r'([一二三四五六日])\s*[~\-～至到]\s*([一二三四五六日])', spec)
    if m:
        a, b = DAYS.index(m.group(1)), DAYS.index(m.group(2))
        out, i = {a}, a
        while i != b:
            i = (i + 1) % 7
            out.add(i)
        return out
    if spec and all(c in DAYS for c in spec):
        return {DAYS.index(c) for c in spec}
    return None


def parse_hours(text):
    """把「一~五 11:00-14:00,17:00-21:00; 六日 10:00-22:00」轉成 {星期(0=一): [(起分鐘, 迄分鐘)]}。

    支援：每天、一~五、六日、一三五；時段用逗號分隔；「24小時」＝整天；「公休」＝全天沒開。
    跨午夜的時段寫成 18:00-02:00。看不懂的片段會被略過；全部看不懂回傳空 dict（＝營業時間不明）。
    """
    table = {}
    for seg in re.split(r'[;；\n]', text or ''):
        seg = seg.strip()
        if not seg:
            continue
        m = re.match(r'^(\S+)\s+(.+)$', seg)
        if not m:
            continue
        days = _day_set(m.group(1))
        if days is None:
            continue
        times = m.group(2).strip()
        spans = []
        if times in ('24小時', '24h', '24H', '全天'):
            spans = [(0, 1440)]
        elif times in ('公休', '休', '休息'):
            spans = []
        else:
            for part in re.split(r'[,，]', times):
                t = re.fullmatch(r'\s*(\d{1,2}):(\d{2})\s*[-~～]\s*(\d{1,2}):(\d{2})\s*', part)
                if not t:
                    spans = None
                    break
                s = int(t.group(1)) * 60 + int(t.group(2))
                e = int(t.group(3)) * 60 + int(t.group(4))
                if e <= s:
                    e += 1440
                spans.append((s, e))
        if spans is None:
            continue
        for d in days:
            table.setdefault(d, []).extend(spans)
    return table


def is_open(text, at=None):
    """True／False；營業時間不明回 None（不當作營業中）。"""
    table = parse_hours(text)
    if not table:
        return None
    at = at or G.now()
    d, m = at.weekday(), at.hour * 60 + at.minute
    for s, e in table.get(d, []):
        if s <= m < e:
            return True
    for s, e in table.get((d - 1) % 7, []):
        if e > 1440 and m + 1440 < e:
            return True
    return False


# ── 價位分層 ──
def tier_of(price):
    lo, hi = C.CFG['food']['price_tiers']
    if price is None:
        return 'daily'
    return 'daily' if price < lo else 'mid' if price < hi else 'high'


# ── 查詢 ──
def _box(lat, meters):
    """回傳 (緯度差, 經度差)，單位是度，保證框得住以該點為中心、半徑 meters 公尺的圓。
    緯度 1 度約 111 公里（除以 100000 已留約 11% 餘裕）；經度 1 度要再除以 cos(緯度)，
    否則高緯度的框會比要求的半徑窄，附近的店會被靜默漏掉。"""
    dlat = meters / 100000
    return dlat, dlat / max(math.cos(math.radians(lat)), 0.02)


COLS = 'id,name,category,address,district,lat,lng,price,rating,reviews,hours,note,tried_at,my_rating'


def _row(r, ctr=None):
    d = None
    if ctr and r[5] is not None and r[6] is not None:
        d = round(G.dist_m(ctr[0], ctr[1], r[5], r[6]))
    op = is_open(r[10])
    return {'id': r[0], 'name': r[1], 'cat': r[2] or '未分類', 'addr': r[3] or '', 'district': r[4] or '',
            'lat': r[5], 'lng': r[6], 'price': r[7], 'rating': r[8], 'reviews': r[9], 'hours': r[10] or '',
            'note': r[11] or '', 'tried': r[12] or '', 'mine': r[13], 'dist': d,
            'tier': tier_of(r[7]), 'open': op}


def resolve_center(near):
    """near：'all'＝不限距離；'gps'＝手機最近的定位；設定檔 places 的名稱＝該地點。回 (中心或 None, 說明)。"""
    if near in ('', 'all', None):
        return None, '全部（不限距離）'
    if near == 'gps':
        try:
            p = dash_commute.latest_point(C.CFG['food']['gps_max_age_minutes'])
        except NotConfigured:
            p = None
        if p:
            return (p['lat'], p['lon']), f"以手機定位為中心（{int(time.time() - p['ts'])} 秒前）"
        return None, f"最近 {C.CFG['food']['gps_max_age_minutes']} 分鐘沒有準的手機定位，改列全部（不限距離）"
    a = G.anchors()
    if near in a:
        return (a[near][0], a[near][1]), f'以「{near}」為中心'
    raise V.Invalid('不認得的中心點：' + str(near)[:20])


def _fetch(db, ctr, radius, district=''):
    sql, args = f'select {COLS} from places where excluded=0', []
    if district:
        sql += ' and (district=? or address like ?)'
        args += [district, f'%{district}%']
    if ctr:
        dlat, dlon = _box(ctr[0], radius)
        sql += ' and lat between ? and ? and lng between ? and ?'
        args += [ctr[0] - dlat, ctr[0] + dlat, ctr[1] - dlon, ctr[1] + dlon]
    rows = [_row(r, ctr) for r in db.execute(sql, args)]
    if ctr:
        rows = [r for r in rows if r['dist'] is not None and r['dist'] <= radius]
    return rows


def _radius(q, default=800):
    """半徑（公尺）：沒填用預設；不是正整數就丟 V.Invalid（HTTP 400）。"""
    return V.integer(q.get('radius'), '半徑', 1, 100000, default=default)


def stats():
    db = connect()
    try:
        q = lambda s: db.execute(s).fetchone()[0]
        return {'places': q('select count(*) from places where excluded=0'),
                'with_hours': q("select count(*) from places where excluded=0 and coalesce(hours,'')<>''"),
                'tried': q("select count(*) from places where excluded=0 and coalesce(tried_at,'')<>''"),
                'excluded': q('select count(*) from places where excluded=1'),
                'categories': q('select count(distinct category) from places where excluded=0')}
    finally:
        db.close()


def districts():
    db = connect()
    try:
        return [{'district': d, 'n': n} for d, n in db.execute(
            "select district,count(*) from places where excluded=0 and coalesce(district,'')<>'' "
            'group by 1 order by 2 desc')]
    finally:
        db.close()


def _filter(rows, q):
    if q.get('tier') in TIER_NAME:
        rows = [r for r in rows if r['tier'] == q['tier']]
    if q.get('cat'):
        rows = [r for r in rows if r['cat'] == q['cat']]
    kw = (q.get('q') or '').strip().lower()
    if kw:
        rows = [r for r in rows if kw in r['name'].lower() or kw in r['addr'].lower()]
    if q.get('tried') == '1':
        rows = [r for r in rows if r['tried']]
    return rows


def cats(q):
    ctr, note = resolve_center(q.get('near', 'gps'))
    db = connect()
    try:
        rows = _fetch(db, ctr, _radius(q), q.get('district', ''))
    finally:
        db.close()
    tiers = {k: sum(1 for r in rows if r['tier'] == k) for k in TIER_NAME}
    tiers['all'] = len(rows)
    rows = _filter(rows, {'tier': q.get('tier')})
    cnt = {}
    for r in rows:
        cnt[r['cat']] = cnt.get(r['cat'], 0) + 1
    return {'total': len(rows), 'cats': sorted(cnt.items(), key=lambda x: -x[1]), 'note': note, 'tiers': tiers}


def listing(q):
    ctr, note = resolve_center(q.get('near', 'gps'))
    db = connect()
    try:
        rows = _filter(_fetch(db, ctr, _radius(q), q.get('district', '')), q)
    finally:
        db.close()
    rows.sort(key=lambda r: (r['dist'] if r['dist'] is not None else 9e9, -(r['reviews'] or 0)))
    return {'total': len(rows), 'rows': rows[:300], 'note': note}


def near(q):
    ctr, note = resolve_center(q.get('near', 'gps'))
    if not ctr:
        raise NotConfigured('手機最近沒有可用的定位（或尚未設定軌跡收集，見 README「通勤軌跡」），無法決定「附近」的中心點')
    radius = min(_radius(q, 300), C.CFG['food']['near_radius_max_m'])
    db = connect()
    try:
        rows = _fetch(db, ctr, radius)
    finally:
        db.close()
    if q.get('open') == '1':
        rows = [r for r in rows if r['open'] is True]
    rows.sort(key=lambda r: r['dist'])
    return {'center': {'lat': ctr[0], 'lon': ctr[1]}, 'radius': radius, 'note': note, 'total': len(rows), 'rows': rows[:15]}


def dice(q, rng=random):
    """隨機選一家。條件：中心點＋半徑（預設以手機定位、800 公尺）、價位上限、類別、價位分層、行政區。
    預設排除飲料／甜點類（勾 drinks=1 才納入）、排除已知「現在沒開」的店（營業時間不明的仍會出現並標註）。"""
    ctr, note = resolve_center(q.get('place', '') or 'gps')
    db = connect()
    try:
        rows = _fetch(db, ctr, _radius(q), q.get('district', ''))
    finally:
        db.close()
    drinks = set(C.CFG['food']['drink_categories'])
    if q.get('drinks') != '1':
        rows = [r for r in rows if r['cat'] not in drinks]
    max_price = V.integer(q.get('price'), '價位上限', 0, 10 ** 7)
    if max_price is not None:
        rows = [r for r in rows if r['price'] is None or r['price'] <= max_price]
    rows = _filter(rows, {'cat': (q.get('cat') or '').strip(), 'tier': q.get('tier')})
    if q.get('open', '1') == '1':
        rows = [r for r in rows if r['open'] is not False]
    if not rows:
        return {'pick': None, 'pool': 0, 'note': note}
    return {'pick': rng.choice(rows), 'pool': len(rows), 'note': note}


def ate():
    db = connect()
    try:
        rows = [_row(r) for r in db.execute(
            f"select {COLS} from places where coalesce(tried_at,'')<>'' order by tried_at desc, id desc")]
    finally:
        db.close()
    by_cat = {}
    for r in rows:
        by_cat[r['cat']] = by_cat.get(r['cat'], 0) + 1
    return {'rows': rows[:200], 'total': len(rows), 'top_cats': sorted(by_cat.items(), key=lambda x: -x[1])[:10]}


def mark_tried(body):
    """POST：{id, date?, rating?}；date 空白＝今天，rating 空白＝不改評分。"""
    pid = V.integer(body.get('id'), '店家編號', 1, 10 ** 9)
    if pid is None:
        raise V.Invalid('店家編號不能空白')
    date = V.date(body['date'], '日期') if body.get('date') else G.today()
    rating = None if body.get('rating') in (None, '') else V.number(body['rating'], '評分', 0, 5)
    require()
    with store_lock(db_path()):
        db = connect(write=True)
        try:
            cur = db.execute('update places set tried_at=?, my_rating=coalesce(?, my_rating) where id=?', (date, rating, pid))
            db.commit()
            if cur.rowcount == 0:
                raise APIError('找不到這家店', 404, 'not_found')
        finally:
            db.close()
    return {'ok': True, 'id': pid, 'date': date}


def nearest_shop(lat, lon, max_m=60):
    """給軌跡推算用：max_m 公尺內最近的店；沒有美食庫或沒有店回 None（不丟例外）。"""
    try:
        db = connect()
    except NotConfigured:
        return None
    try:
        dlat, dlon = _box(lat, max_m)
        best = None
        for r in db.execute('select name,lat,lng from places where excluded=0 and lat between ? and ? and lng between ? and ?',
                            (lat - dlat, lat + dlat, lon - dlon, lon + dlon)):
            m = G.dist_m(lat, lon, r[1], r[2])
            if m <= max_m and (best is None or m < best['dist']):
                best = {'name': r[0], 'dist': round(m)}
        return best
    except sqlite3.Error:
        return None
    finally:
        db.close()
