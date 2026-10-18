"""臺北捷運：列車到站倒數、車廂擁擠度（臺北捷運公司 API，SOAP）。

使用前必須自行向臺北捷運公司申請 API 會員取得帳號密碼（見 README「需要自己申請的東西」）。
帳密只從環境變數或權限 600 的檔案讀取，只放在送給捷運 API 的請求本文裡（已做 XML 跳脫），
不寫進日誌、不放進回應、不寫進任何錯誤訊息。依官方規範，帳密不得提供給第三方：
請不要把自己的帳密設定在別人使用的儀表板上，也不要把這個儀表板開放給不信任的人。

三支用到的 API（回應格式實測如下，官方文件沒有說明欄位意義）：
  TrackInfo.getTrackInfo          列車到站：StationName、DestinationName、CountDown（mm:ss、「列車進站」、「資料擷取中」…）
  CarWeight.getCarWeightByInfoEx  高運量車廂擁擠度：TrainNumber、StationID（站碼）、Cart1L～Cart6L（等級）…
  CarWeightBR.getCarWeightBRInfo  文湖線車廂擁擠度：TrainNumber、StationID、DU（上行／下行）、Car1～Car4（等級）…
回應有兩種長相：有的是 JSON 陣列後面直接接一個空的 SOAP 信封，有的是標準 SOAP 把 JSON 字串放在結果元素裡；這裡兩種都處理。
"""
import datetime
import hashlib
import json
import math
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

import dash_config as C
import dash_geo as G
from dash_ha import _NoRedirect
from dash_support import APIError, NotConfigured, TimedCache

ENDPOINTS = {'track': ('TrackInfo.asmx', 'getTrackInfo'),
             'weight': ('CarWeight.asmx', 'getCarWeightByInfoEx'),
             'weight_br': ('CarWeightBR.asmx', 'getCarWeightBRInfo')}
STATION_LIST_METHOD = 'GetStationList'
HIGH_CAPACITY = ('R', 'BL', 'G', 'O')      # 高運量擁擠度 API 涵蓋的路線代碼（環狀線等沒有擁擠度資料）
BR = 'BR'                                   # 文湖線
LEVEL_TEXT = {1: '寬鬆', 2: '適中', 3: '擁擠'}
LEVEL_NOTE = ('官方文件沒有定義擁擠等級的意義。等級 1／2／3 對應「寬鬆／適中／擁擠」是依資料推斷（實際資料裡絕大多數是 1，少數 2、更少 3），'
              '僅供參考；看到其他數字就只顯示原始等級。')
FAIL_TTL = 30          # 一般失敗（逾時、格式不對、HTTP 錯誤…）也記住幾秒，避免出錯時每個請求都去敲捷運伺服器
AUTH_FAIL_TTL = 300    # 帳密被拒絕（HTTP 401／403、SOAP Fault 或結果文字是認證錯誤）時冷卻 5 分鐘，避免重複送錯誤帳密而被鎖；
                       # 冷卻以「這組帳密」為單位，三支即時 API 共用；換了帳密就不受影響
# 資料新鮮度（「本地策略」，不是官方規定；官方文件沒有說明更新頻率）：
ARRIVAL_MAX_AGE = 120   # 到站資料的時間距今超過這麼多秒，就標「資料已過期」，不再顯示倒數
ARRIVING_MAX_AGE = 60   # 「列車進站」只在資料這麼新時才算數
CROWD_MAX_AGE = 600     # 擁擠度資料時間距今超過這麼多秒，就不當成「目前」顯示（並提示被隱藏幾列）
FUTURE_SLACK = 60       # 資料時間比現在還晚超過這麼多秒，視為時間異常
TZ_TAIPEI = datetime.timezone(datetime.timedelta(hours=8))    # 捷運 API 的時間一律是臺北時間
AUTH_WORDS = ('認證', '驗證', '帳號', '密碼', '帳密', '授權', '權限', '登入', 'auth', 'password', 'passwd', 'credential', 'unauthor',
              'forbidden', 'denied', 'invalid user', 'invalid account', 'login', 'permission')
AUTH_MESSAGE = '臺北捷運 API 拒絕了這組帳號密碼，請確認是否正確、是否仍有使用權限'
NEARBY_NOTE = '「附近」是依站碼推斷：同一條路線代碼、站序在常用站前後幾站內（分支線與跨線轉乘站可能不準），僅供參考。'
ARRIVAL_NOTE = ('捷運 API 通常每個站、每個終點只提供最近的一班車，所以多半只會看到一筆。倒數依資料時間扣減，資料超過 2 分鐘沒更新就標「資料已過期」；'
                'API 沒有路線欄位，同一終點出現多筆時無法確認是不是同一路線，會標「路線未區分」。')

_cache = TimedCache(limit=8)
_stations = TimedCache(limit=1)
_fail = {}            # API 名稱 -> (冷卻到的時間, 訊息, 狀態碼, 錯誤碼)：一般失敗
_auth_fail = {}       # 帳密指紋 -> 同上：認證被拒絕（跨 API 共用）
_locks = {}
_locks_guard = threading.Lock()
_mono = time.monotonic                       # 測試可替換
_wall = lambda: datetime.datetime.now(TZ_TAIPEI)


def _lock(name):
    with _locks_guard:
        return _locks.setdefault(name, threading.Lock())


def _fingerprint(creds):
    """帳密的雜湊（只存在記憶體，用來辨認「同一組帳密」，不含可還原的內容）。"""
    return hashlib.sha256(('\0'.join(creds)).encode('utf-8')).hexdigest()


# ── 設定與憑證 ──
def cfg():
    return C.CFG['metro']


def credentials():
    """(帳號, 密碼)；沒設定丟 NotConfigured（帶說明要去申請）；設定有問題也丟 NotConfigured（原因不含帳密）。"""
    try:
        acc, pw = C.metro_credentials()
    except C.ConfigError as e:
        raise NotConfigured(str(e)) from None
    if not acc or not pw:
        raise NotConfigured('尚未設定臺北捷運 API 帳號密碼：這項功能需要自己向臺北捷運公司申請 API 會員（見 README「需要自己申請的東西」），'
                            '取得後用環境變數 METRO_ACCOUNT／METRO_PASSWORD 或權限 600 的檔案（設定檔 metro.credentials_file）提供')
    return acc, pw


def configured_reason():
    """給功能狀態用：回傳「還沒好」的原因，好了回空字串。不連網。"""
    try:
        credentials()
    except NotConfigured as e:
        return e.reason
    if not cfg()['stations']:
        return '尚未在設定檔 metro.stations 填入常用站（例如 ["台北車站"]）'
    return ''


def _require():
    credentials()
    if not cfg()['stations']:
        raise NotConfigured('尚未在設定檔 metro.stations 填入常用站（例如 ["台北車站"]）')


# ── SOAP 請求 ──
def build_envelope(method, creds=None):
    """SOAP 1.1 請求本文。帳密一定經過 XML 跳脫（& < > 不會破壞結構、也無法注入元素）。"""
    inner = ''
    if creds:
        inner = f'<userName>{escape(creds[0])}</userName><passWord>{escape(creds[1])}</passWord>'
    return ('<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
            f'<{method} xmlns="http://tempuri.org/">{inner}</{method}></soap:Body></soap:Envelope>').encode('utf-8')


def _bad(msg):
    return APIError(f'臺北捷運 API 回應格式不是預期的（{msg}）', 502, 'metro_bad_response')


def _looks_like_auth_error(text):
    t = str(text or '').lower()
    return any(w in t for w in AUTH_WORDS)


def _text_error(text):
    """HTTP 200 但回的是文字而不是資料：看起來像認證／權限問題就歸為認證被拒絕（長冷卻），
    否則歸為「內容無法辨識」。上游的原文一律不放進錯誤訊息、不寫日誌（只用固定的中文說明）。"""
    if _looks_like_auth_error(text):
        return APIError(AUTH_MESSAGE, 502, 'metro_auth')
    return APIError('臺北捷運 API 回應的內容無法辨識，不是資料（原因未明）', 502, 'metro_unknown')


def _strings(v, depth=0):
    if isinstance(v, str):
        yield v
    elif depth < 4 and isinstance(v, dict):
        for x in v.values():
            yield from _strings(x, depth + 1)
    elif depth < 4 and isinstance(v, list):
        for x in v:
            yield from _strings(x, depth + 1)


def parse_rows(raw):
    """回應本文（bytes）→ list[dict]。兩種資料格式都收；SOAP Fault、結果是文字（含認證錯誤）、空結果、非 JSON 都轉成固定的 APIError。"""
    text = raw.decode('utf-8-sig', 'replace').lstrip()
    if text.startswith('['):
        try:
            arr, _ = json.JSONDecoder().raw_decode(text)         # 後面接的空 SOAP 信封直接忽略
        except ValueError:
            raise _bad('資料不是合法的 JSON') from None
    elif not text.startswith('<'):
        # 直接回文字、或回 JSON 字串／物件（不是資料陣列）
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):                          # 例如前面有雜訊的陣列（不會發生在已知格式，保守處理）
                arr = parsed
                return [r for r in arr if isinstance(r, dict)]
            text = ' '.join(_strings(parsed)) or text
        except ValueError:
            pass
        raise _text_error(text)
    else:
        if re.search(r'<!\s*(DOCTYPE|ENTITY)', text, re.I):
            raise _bad('含有不允許的 XML 宣告')                  # 不處理 DTD／外部實體
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            raise _bad('不是合法的 XML') from None
        for el in root.iter():
            if el.tag.split('}')[-1].lower() == 'faultstring':
                # Fault 的原文不回顯、不記錄：只判斷類別，回固定訊息
                if _looks_like_auth_error(el.text):
                    raise APIError(AUTH_MESSAGE, 502, 'metro_auth')
                raise APIError('臺北捷運 API 回報了錯誤（SOAP Fault，原因未明）；請稍後再試，持續發生請確認帳號是否仍有使用權限', 502, 'metro_fault')
        payload, other = None, ''
        for el in root.iter():
            if not el.text or not el.text.strip():
                continue
            if el.text.lstrip().startswith('['):
                payload = el.text
                break
            if el.tag.split('}')[-1].lower().endswith('result'):
                other = el.text
        if payload is None:
            if other:
                raise _text_error(other)                          # 結果元素裡是文字（可能是認證錯誤）
            raise _bad('沒有資料')
        try:
            arr = json.loads(payload)
        except ValueError:
            raise _bad('資料不是合法的 JSON') from None
    if not isinstance(arr, list):
        raise _bad('資料不是清單')
    return [r for r in arr if isinstance(r, dict)]


def _post(url, method, creds=None):
    m = cfg()
    req = urllib.request.Request(url, data=build_envelope(method, creds), headers={'Content-Type': 'text/xml; charset=utf-8'})
    opener = urllib.request.build_opener(_NoRedirect)           # 不跟隨轉址（帳密在請求本文裡，不能被轉去別處）
    try:
        with opener.open(req, timeout=m['timeout_seconds']) as r:
            raw = r.read(m['max_response_bytes'] + 1)
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            raise APIError('臺北捷運 API 回應了重新導向，為了保護帳密已拒絕跟隨', 502, 'metro_redirect') from None
        if e.code in (401, 403):
            raise APIError(AUTH_MESSAGE + '（401/403）', 502, 'metro_auth') from None
        raise APIError(f'臺北捷運 API 回應 HTTP {e.code}', 502, 'metro_http') from None
    except (socket.timeout, TimeoutError):
        raise APIError(f'連線臺北捷運 API 逾時（{m["timeout_seconds"]} 秒）', 504, 'metro_timeout') from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        if isinstance(getattr(e, 'reason', None), (socket.timeout, TimeoutError)):
            raise APIError(f'連線臺北捷運 API 逾時（{m["timeout_seconds"]} 秒）', 504, 'metro_timeout') from None
        raise APIError(f'連不到臺北捷運 API（{type(e).__name__}）', 502, 'metro_unreachable') from None
    if len(raw) > m['max_response_bytes']:
        raise APIError('臺北捷運 API 的回應超過大小上限，已丟棄', 502, 'metro_too_large')
    return raw


def _fetch(name, creds):
    url, method = ENDPOINTS[name]
    return parse_rows(_post(cfg()['api_base'].rstrip('/') + '/' + url, method, creds))


def rows(name):
    """取某支 API 的資料。同一支 API 的所有請求排隊通過同一把鎖：
    取得鎖後才「查冷卻 → 查快取 → 發送 → 記成功／失敗」，所以並行請求不會各自送一次（不論成功或失敗，上游只被問一次）；
    不同支 API 之間仍可並行。成功結果快取 cache_seconds 秒（至少 30）；失敗與認證被拒絕依上面的冷卻規則，
    冷卻從「收到結果的那一刻」起算；認證被拒絕的冷卻以帳密為單位，三支 API 共用。"""
    creds = credentials()                     # 沒設定就直接丟 NotConfigured，不進快取、不冷卻
    fp = _fingerprint(creds)
    with _lock(name):
        now = _mono()
        for rec in (_auth_fail.get(fp), _fail.get(name)):
            if rec and rec[0] > now:
                raise APIError(rec[1], rec[2], rec[3])
        try:
            return _cache.get(name, max(30, cfg()['cache_seconds']), lambda: _fetch(name, creds))
        except NotConfigured:
            raise
        except APIError as e:
            done = _mono()
            if e.code == 'metro_auth':
                _auth_fail[fp] = (done + AUTH_FAIL_TTL, str(e), e.status, e.code)
            else:
                _fail[name] = (done + FAIL_TTL, str(e), e.status, e.code)
            raise


def station_list():
    """車站代碼表（不需要帳密的公開查詢），快取 24 小時。回傳 ({正規化站名: [站碼…]}, {站碼: 站名})。"""
    def build():
        arr = parse_rows(_post(cfg()['station_list_url'], STATION_LIST_METHOD))
        by_name, by_code = {}, {}
        for r in arr:
            code, name = r.get('SCODE'), r.get('SCNAME')
            if isinstance(code, str) and isinstance(name, str) and _code(code):
                by_name.setdefault(norm(name), []).append(code)
                by_code[code] = name
        if not by_code:
            raise _bad('車站代碼表是空的')
        return by_name, by_code
    with _lock('station_list'):
        f = _fail.get('station_list')
        if f and f[0] > _mono():
            raise APIError(f[1], f[2], f[3])
        try:
            return _stations.get('list', 86400, build)
        except APIError as e:
            _fail['station_list'] = (_mono() + FAIL_TTL, str(e), e.status, e.code)
            raise


# ── 站名與站碼 ──
def norm(name):
    """站名比對用的正規化：臺→台、去前後空白、去掉一個結尾的「站」。兩邊（設定檔與 API 資料）都這樣處理，所以「板橋」與「板橋站」對得上。"""
    n = str(name or '').strip().replace('臺', '台')
    return n[:-1] if n.endswith('站') else n


def _code(code):
    """'BL12' → ('BL', 12)；'R22A' → ('R', 22)；非一般站碼（ARENA、BL07MALL…）回 None。"""
    m = re.fullmatch(r'([A-Z]{1,2})(\d{2})[A-Z]?', code or '')
    return (m.group(1), int(m.group(2))) if m else None


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


# ── 列車到站 ──
def parse_time(v):
    """捷運 API 的時間字串（臺北時間，例 2026-10-19 13:04:46）→ 有時區的 datetime；格式不對回 None。"""
    if not isinstance(v, str):
        return None
    try:
        return datetime.datetime.fromisoformat(v.strip().replace('/', '-')).replace(tzinfo=TZ_TAIPEI)
    except ValueError:
        return None


def _age(dt, now):
    return (now - dt).total_seconds() if dt else None


def parse_countdown(text):
    t = str(text or '').strip()
    m = re.fullmatch(r'(\d{1,2}):(\d{2})', t)
    if m:
        sec = int(m.group(1)) * 60 + int(m.group(2))
        return {'state': 'countdown', 'seconds': sec, 'text': t}
    if t == '列車進站':
        return {'state': 'arriving', 'seconds': 0, 'text': t}
    if t in ('資料擷取中', '營運時間已過'):
        return {'state': 'pending' if t == '資料擷取中' else 'closed', 'seconds': None, 'text': t}
    return {'state': 'unknown', 'seconds': None, 'text': t[:20]}


def _apply_freshness(item, age):
    """依資料時間距今多久，調整一筆到站資料：
      * 沒有時間、時間在未來太多、超過 ARRIVAL_MAX_AGE 沒更新 → stale（資料已過期，不顯示倒數）
      * 倒數：扣掉資料已經過的秒數；扣到 0 以下 → waiting（等待更新），絕不會變成「列車進站」
      * 「列車進站」超過 ARRIVING_MAX_AGE 就 stale
    item['seconds'] 是「現在」還剩幾秒（前端再從收到回應的時刻繼續往下扣）。"""
    item['age'] = None if age is None else round(age)
    if age is None:
        item.update(state='stale', seconds=None, text='資料時間不明', why='no_time')
    elif age < -FUTURE_SLACK:
        item.update(state='stale', seconds=None, text='資料時間異常', why='future')
    elif age > ARRIVAL_MAX_AGE:
        item.update(state='stale', seconds=None, text='資料已過期', why='old')
    elif item['state'] == 'arriving' and age > ARRIVING_MAX_AGE:
        item.update(state='stale', seconds=None, text='資料已過期', why='old')
    elif item['state'] == 'countdown':
        left = item['seconds'] - max(age, 0)
        if left <= 0:
            item.update(state='waiting', seconds=0, text='等待更新')
        else:
            item['seconds'] = int(math.ceil(left))
    if item['state'] != 'stale':
        # 這筆資料還能再「新鮮」幾秒（保守取整數秒）：前端在沒有新回應時，自己依經過的時間撤下過期的值
        limit = ARRIVING_MAX_AGE if item['state'] == 'arriving' else ARRIVAL_MAX_AGE
        item['expires_in'] = max(0, int(math.floor(limit - max(age, 0))))
    return item


def arrivals():
    _require()
    data = rows('track')
    now = _wall()
    by = {}
    for r in data:
        by.setdefault(norm(r.get('StationName')), []).append(r)
    out = []
    for st in cfg()['stations']:
        rs = by.get(norm(st))
        if not rs:
            out.append({'name': st, 'found': False, 'directions': []})
            continue
        dirs, times = {}, []
        for r in rs:
            dt = parse_time(r.get('NowDateTime'))
            if dt:
                times.append(dt)
            item = _apply_freshness(parse_countdown(r.get('CountDown')), _age(dt, now))
            item['train'] = str(r.get('TrainNumber') or '')[:12]
            item['time'] = dt.strftime('%H:%M:%S') if dt else ''
            dirs.setdefault(str(r.get('DestinationName') or '未知方向')[:20], []).append(item)
        directions = []
        for to, items in sorted(dirs.items()):
            items.sort(key=lambda x: (x['state'] != 'countdown', x['seconds'] if x['seconds'] is not None else 10 ** 9))
            # 同站同終點出現多筆：API 沒有路線欄位，可能是不同路線，不能當成「下一班、下下班」
            directions.append({'to': to, 'trains': items[:2], 'ambiguous_line': len(items) > 1, 'count': len(items)})
        usable = [t for d in directions for t in d['trains'] if t['state'] not in ('stale',)]
        out.append({'name': st, 'found': True, 'directions': directions, 'all_stale': not usable,
                    'updated': max(times).strftime('%Y-%m-%d %H:%M:%S') if times else ''})
    return {'stations': out, 'note': ARRIVAL_NOTE, 'fetched': now.strftime('%H:%M:%S'), 'max_age': ARRIVAL_MAX_AGE}


# ── 車廂擁擠度 ──
def _levels(row, keys):
    return [_int(row.get(k)) for k in keys]


def _near(row_code, targets, nearby):
    """row_code 是否在任一目標站碼前後 nearby 站內（同路線代碼）。回 (距離, 偏移) 或 None。"""
    rc = _code(row_code)
    if not rc:
        return None
    best = None
    for tc in targets:
        if tc[0] == rc[0] and abs(rc[1] - tc[1]) <= nearby:
            cand = (abs(rc[1] - tc[1]), rc[1] - tc[1])
            if best is None or cand < best:
                best = cand
    return best


def _fresh_crowd(time_text, now):
    """擁擠度資料的新鮮度：回 (是否可當「目前」顯示, 資料距今秒數或 None)。"""
    age = _age(parse_time(time_text), now)
    if age is None or age < -FUTURE_SLACK or age > CROWD_MAX_AGE:
        return False, None if age is None else round(age)
    return True, round(max(age, 0))


def crowding():
    _require()
    by_name, by_code = station_list()
    nearby = cfg()['nearby_stations']
    now = _wall()
    targets = {}
    unknown = []
    for st in cfg()['stations']:
        codes = [c for c in by_name.get(norm(st), []) if _code(c)]
        if not codes:
            unknown.append(st)
        targets[st] = [_code(c) for c in codes]
    need_high = any(t[0] in HIGH_CAPACITY for ts in targets.values() for t in ts)
    need_br = any(t[0] == BR for ts in targets.values() for t in ts)
    sources, errors = {}, []
    for key, needed, label in (('weight', need_high, '高運量'), ('weight_br', need_br, '文湖線')):
        if not needed:
            continue
        try:
            sources[key] = rows(key)
        except NotConfigured:
            raise
        except APIError as e:
            errors.append(f'{label}：{e}')
    stations = []
    for st in cfg()['stations']:
        tcs = targets[st]
        trains, hidden = [], 0
        for r in sources.get('weight', []):
            near = _near(r.get('StationID'), [t for t in tcs if t[0] in HIGH_CAPACITY], nearby)
            if not near:
                continue
            ok, age = _fresh_crowd(r.get('utime'), now)
            if not ok:
                hidden += 1
                continue
            trains.append({'line': '高運量', 'train': str(r.get('TrainNumber') or '')[:12], 'cars': str(r.get('CN1') or '')[:20],
                           'at': r.get('StationID'), 'at_name': by_code.get(r.get('StationID')), 'offset': near[1], 'dist': near[0],
                           'direction': f'方向代碼 {r.get("CID")}（意義官方未公布）' if r.get('CID') is not None else '',
                           'levels': _levels(r, [f'Cart{i}L' for i in range(1, 7)]), 'updated': str(r.get('utime') or '')[:25], 'age': age,
                           'expires_in': max(0, CROWD_MAX_AGE - age)})
        for r in sources.get('weight_br', []):
            near = _near(r.get('StationID'), [t for t in tcs if t[0] == BR], nearby)
            if not near:
                continue
            ok, age = _fresh_crowd(r.get('UpdateTime'), now)
            if not ok:
                hidden += 1
                continue
            trains.append({'line': '文湖線', 'train': str(r.get('TrainNumber') or '')[:12], 'cars': '',
                           'at': r.get('StationID'), 'at_name': by_code.get(r.get('StationID')), 'offset': near[1], 'dist': near[0],
                           'direction': str(r.get('DU') or '')[:6],
                           'levels': _levels(r, [f'Car{i}' for i in range(1, 5)]), 'updated': str(r.get('UpdateTime') or '')[:25], 'age': age,
                           'expires_in': max(0, CROWD_MAX_AGE - age)})
        trains.sort(key=lambda x: (x['dist'], x['line'], x['train']))
        covered = bool(tcs) and any(t[0] in HIGH_CAPACITY or t[0] == BR for t in tcs)
        stations.append({'name': st, 'found': st not in unknown, 'covered': covered, 'stale_hidden': hidden,
                         'codes': sorted(c for c in by_name.get(norm(st), []) if _code(c)), 'trains': trains[:12]})
    return {'stations': stations, 'errors': errors, 'level_text': LEVEL_TEXT, 'level_note': LEVEL_NOTE, 'nearby': nearby,
            'nearby_note': NEARBY_NOTE, 'fetched': now.strftime('%H:%M:%S'), 'max_age': CROWD_MAX_AGE}


def reset():
    """測試用：清快取與失敗紀錄。"""
    _cache.data.clear()
    _stations.data.clear()
    _fail.clear()
    _auth_fail.clear()
