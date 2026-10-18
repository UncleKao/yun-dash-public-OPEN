"""通勤／軌跡：讀 HA device_tracker 收集到的定位點，畫某一天的軌跡並推算停留與移動。

資料來源是 tools/loc_collect.py 寫的 SQLite（data/locations.sqlite）。本模組只讀。
推算規則（都可在設定檔 commute 區調整）：
  * 定位誤差超過 max_accuracy_m 的點不用。
  * 連續的點都落在 stay_radius_m 內，且持續 stay_min_minutes 以上 → 一段「停留」。
  * 停留之間的移動，依平均速度分類：>= 8 km/h 搭乘交通工具、>= 2 km/h 走路、其餘慢慢移動。
  * 停留的名稱：先比對設定檔 places 的常用地點；其次（若有美食庫）60 公尺內最近的店；
    都沒有就顯示座標。
這是「事後依軌跡推算」，不是即時的交通判斷，也不會知道搭的是哪一班車。
"""
import datetime
import os
import sqlite3
import statistics
import time

import dash_config as C
import dash_geo as G
from dash_support import NotConfigured

MODE_LABEL = {'walk': '走路', 'vehicle': '搭乘交通工具', 'slow': '慢慢移動'}


def db_path():
    return C.data_path('locations.sqlite')


def require():
    if not C.CFG['commute']['entities']:
        raise NotConfigured('尚未設定 commute.entities（要追蹤的 Home Assistant device_tracker 實體）')
    if not os.path.exists(db_path()):
        raise NotConfigured('還沒有軌跡資料庫：請先執行 tools/loc_collect.py 開始收集定位（見 README）')


def _connect():
    return sqlite3.connect(f'file:{db_path()}?mode=ro', uri=True)


def day_points(date):
    """某天所有採用的定位點：[{'t','lat','lon','acc','src'}]，依時間排序。"""
    require()
    tz = G.tz()
    d0 = datetime.datetime.combine(datetime.date.fromisoformat(date), datetime.time(0), tz).timestamp()
    cfg = C.CFG['commute']
    pts, seen = [], set()
    db = _connect()
    try:
        for i, ent in enumerate(cfg['entities']):
            for u, la, lo, acc in db.execute(
                    'select ha_last_updated,lat,lon,gps_accuracy from locations '
                    'where entity=? and poll_ts between ? and ?', (ent, d0, d0 + 86400 + 600)):
                if la is None or lo is None or (acc if acc is not None else 999) > cfg['max_accuracy_m']:
                    continue
                t = datetime.datetime.fromisoformat(u).astimezone(tz)
                k = (i, round(t.timestamp()))
                if k in seen or not d0 <= t.timestamp() < d0 + 86400:
                    continue
                seen.add(k)
                pts.append({'t': t.strftime('%H:%M:%S'), 'dt': t, 'lat': la, 'lon': lo, 'acc': acc or 0,
                            'src': 'main' if i == 0 else 'sub'})
    finally:
        db.close()
    pts.sort(key=lambda p: p['dt'])
    return pts


def latest_point(max_age_min):
    """最近 max_age_min 分鐘內、誤差夠小的最後一個點（主要實體優先）；沒有回 None。"""
    require()
    cfg = C.CFG['commute']
    db = _connect()
    try:
        for ent in cfg['entities']:
            row = db.execute('select poll_ts,lat,lon,gps_accuracy from locations where entity=? and lat is not null '
                             'and gps_accuracy<=? and poll_ts between ? and ? order by poll_ts desc limit 1',
                             (ent, cfg['max_accuracy_m'], time.time() - max_age_min * 60, time.time() + 60)).fetchone()
            if row:
                return {'ts': row[0], 'lat': row[1], 'lon': row[2], 'acc': row[3]}
    finally:
        db.close()
    return None


def _hm(dt):
    return dt.strftime('%H:%M')


def _center(cluster):
    return statistics.median(p['lat'] for p in cluster), statistics.median(p['lon'] for p in cluster)


def cluster_stays(pts, radius):
    """連續的點彼此都在 radius 內 → 同一群。"""
    groups, cur = [], []
    for p in pts:
        if cur and max(G.dist_m(p['lat'], p['lon'], q['lat'], q['lon']) for q in cur) > radius:
            groups.append(cur)
            cur = []
        cur.append(p)
    if cur:
        groups.append(cur)
    # 定位飄移會把同一個停留切成兩段：相隔 <= 10 分鐘、中心 <= 80 公尺就併回去
    merged = []
    for g in groups:
        if (merged and len(merged[-1]) > 1 and len(g) > 1
                and (g[0]['dt'] - merged[-1][-1]['dt']).total_seconds() <= 600
                and G.dist_m(*_center(merged[-1]), *_center(g)) <= 80):
            merged[-1] = merged[-1] + g
        else:
            merged.append(g)
    return merged


def classify(kmh):
    return 'vehicle' if kmh >= 8 else 'walk' if kmh >= 2 else 'slow'


def label_place(lat, lon, nearest_shop=None):
    """回傳 (名稱, 圖示或 None)。"""
    best = None
    for name, (la, lo, r, icon) in G.anchors().items():
        d = G.dist_m(lat, lon, la, lo)
        if d <= r and (best is None or d < best[0]):
            best = (d, name, icon)
    if best:
        return best[1], best[2]
    if nearest_shop:
        shop = nearest_shop(lat, lon)
        if shop:
            return f"{shop['name']}({shop['dist']}m)", '🍽️'
    return f'{lat:.4f},{lon:.4f}', '📍'


def build_segments(pts, nearest_shop=None, cfg=None):
    """pts: day_points() 的結果。回傳時間軸 [{'kind':'stay'|'move', ...}]。"""
    cfg = cfg or C.CFG['commute']
    if not pts:
        return []
    groups = cluster_stays(pts, cfg['stay_radius_m'])
    min_s = cfg['stay_min_minutes'] * 60
    stays = []
    for i, g in enumerate(groups):
        dur = (g[-1]['dt'] - g[0]['dt']).total_seconds()
        if len(g) > 1 and (dur >= min_s or i in (0, len(groups) - 1)):
            stays.append(g)
    segs = []
    prev_end = None
    for g in stays:
        a, b = g[0], g[-1]
        if prev_end is not None:
            _add_move(segs, pts, prev_end, a)
        la, lo = _center(g)
        name, icon = label_place(la, lo, nearest_shop)
        mins = round((b['dt'] - a['dt']).total_seconds() / 60)
        if segs and segs[-1]['kind'] == 'stay' and segs[-1]['place'] == name:
            segs[-1]['b'], segs[-1]['mins'] = _hm(b['dt']), segs[-1]['mins'] + mins
        else:
            segs.append({'kind': 'stay', 'a': _hm(a['dt']), 'b': _hm(b['dt']), 'mins': mins,
                         'place': name, 'icon': icon, 'lat': la, 'lon': lo})
        prev_end = b
    return segs


def _add_move(segs, pts, p0, p1):
    gap = (p1['dt'] - p0['dt']).total_seconds() / 60
    if gap < 1:
        return
    between = [p for p in pts if p0['dt'] <= p['dt'] <= p1['dt']]
    if len(between) < 2:
        between = [p0, p1]
    meters = sum(G.dist_m(x['lat'], x['lon'], y['lat'], y['lon']) for x, y in zip(between, between[1:]))
    kmh = meters / 1000 / gap * 60
    mode = classify(kmh)
    if mode == 'slow' and meters < 100:
        return
    segs.append({'kind': 'move', 'a': _hm(p0['dt']), 'b': _hm(p1['dt']), 'km': round(meters / 1000, 1),
                 'mins': round(gap), 'kmh': round(kmh), 'mode': mode, 'label': MODE_LABEL[mode]})


def track(date, nearest_shop=None):
    pts = day_points(date)
    segs = build_segments([p for p in pts if p['src'] == 'main'] or pts, nearest_shop)
    now = G.now()
    out_pts = [{k: v for k, v in p.items() if k != 'dt'} for p in pts]
    return {'date': date, 'points': out_pts, 'segs': segs,
            'now': out_pts[-1] if out_pts and date == now.strftime('%Y-%m-%d') else None}
