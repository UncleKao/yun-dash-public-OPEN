"""地理與時間的小工具。"""
import datetime
import math

import dash_config as C


def tz():
    return datetime.timezone(datetime.timedelta(hours=C.CFG['utc_offset_hours']))


def now():
    return datetime.datetime.now(tz())


def today():
    return now().strftime('%Y-%m-%d')


def dist_m(la1, lo1, la2, lo2):
    """兩點之間的球面距離（公尺）。"""
    p = math.radians
    x = math.sin(p(la2 - la1) / 2) ** 2 + math.cos(p(la1)) * math.cos(p(la2)) * math.sin(p(lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(x))


def anchors():
    """設定檔 places → {名稱: (lat, lon, radius_m, icon)}。"""
    out = {}
    for name, p in C.CFG['places'].items():
        out[name] = (float(p['lat']), float(p['lon']), float(p.get('radius', 100)), p.get('icon') or '📍')
    return out
