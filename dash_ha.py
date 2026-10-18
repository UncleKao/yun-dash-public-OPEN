"""Home Assistant REST 的最小客戶端（只用標準庫）。只讀取，不寫入任何 HA 狀態。"""
import datetime
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request

import dash_config as C
from dash_support import APIError, NotConfigured


def token_problem():
    """權杖設定本身有問題（檔案權限太鬆、讀不到）時回原因字串，否則回空字串。"""
    try:
        C.ha_token()
    except C.ConfigError as e:
        return str(e)
    return ''


def configured():
    try:
        return bool(C.CFG['ha'].get('url') and C.ha_token())
    except C.ConfigError:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """帶著 Bearer 權杖的請求絕不跟隨重新導向（避免權杖被送往別的來源）。"""

    def redirect_request(self, *a, **kw):
        return None


def _ctx():
    h = C.CFG['ha']
    if h.get('ca_file'):
        try:
            return ssl.create_default_context(cafile=os.path.expanduser(h['ca_file']))
        except (OSError, ssl.SSLError) as e:
            raise NotConfigured(f'讀不到或不合法的 CA 憑證檔 ha.ca_file（{h["ca_file"]}）：{getattr(e, "strerror", None) or e}') from None
    ctx = ssl.create_default_context()
    if not h.get('verify_ssl', True):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


MAX_BYTES = 20 * 1024 * 1024     # 回應大小上限（/api/states 在很大的安裝上也不會超過）


def get(path, timeout=None):
    """GET HA 的 /api/... 路徑，回傳解析後的 JSON。"""
    if not C.CFG['ha'].get('url'):
        raise NotConfigured('尚未設定 Home Assistant 網址（設定檔 ha.url）')
    try:
        token = C.ha_token()
    except C.ConfigError as e:
        raise NotConfigured(str(e)) from None
    if not token:
        raise NotConfigured('尚未設定 Home Assistant 權杖（環境變數 HA_TOKEN 或設定檔 ha.token_file）')
    url = C.CFG['ha']['url'].rstrip('/') + path
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token})
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=_ctx()))
    try:
        with opener.open(req, timeout=timeout or C.CFG['ha'].get('timeout_seconds', 20)) as r:
            raw = r.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise APIError('Home Assistant 的回應超過大小上限，已丟棄', 502, 'ha_too_large')
        return json.loads(raw)
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            raise APIError('Home Assistant 回應了重新導向，為了保護權杖已拒絕跟隨；請把設定檔 ha.url 改成最終網址', 502, 'ha_redirect') from None
        if e.code in (401, 403):
            raise APIError('Home Assistant 拒絕這組權杖（401/403），請確認長期存取權杖是否有效', 502, 'ha_auth') from None
        if e.code == 404:
            raise APIError('Home Assistant 找不到這個實體或路徑（404），請確認實體 ID', 502, 'ha_not_found') from None
        raise APIError(f'Home Assistant 回應 HTTP {e.code}', 502, 'ha_http') from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        if isinstance(e, TimeoutError) or isinstance(getattr(e, 'reason', None), TimeoutError):
            raise APIError(f'連線 Home Assistant 逾時（{timeout or C.CFG["ha"].get("timeout_seconds", 20)} 秒）', 504, 'ha_timeout') from None
        raise APIError(f'連不到 Home Assistant（{type(e).__name__}）', 502, 'ha_unreachable') from None


def state(entity):
    d = get('/api/states/' + urllib.parse.quote(entity, safe='._'))
    if not isinstance(d, dict):
        raise APIError('Home Assistant 回傳的資料格式不是預期的物件', 502, 'ha_shape')
    return d


def states():
    d = get('/api/states')
    if not isinstance(d, list):
        raise APIError('Home Assistant 回傳的資料格式不是預期的清單', 502, 'ha_shape')
    return [x for x in d if isinstance(x, dict) and isinstance(x.get('entity_id'), str)]


def history(entities, start, end=None, timeout=None):
    """多個實體的歷史（minimal_response）。回傳 {entity_id: [{state, last_changed}, ...]}。"""
    end = end or datetime.datetime.now(datetime.timezone.utc)
    q = urllib.parse.urlencode({'filter_entity_id': ','.join(entities), 'end_time': end.isoformat(),
                                'minimal_response': '', 'no_attributes': ''})
    raw = get('/api/history/period/' + urllib.parse.quote(start.isoformat()) + '?' + q, timeout=timeout)
    if raw is not None and not isinstance(raw, list):
        raise APIError('Home Assistant 回傳的歷史資料格式不是預期的清單', 502, 'ha_shape')
    out = {}
    for series in raw or []:
        if not isinstance(series, list) or not series or not isinstance(series[0], dict):
            continue
        # minimal_response 只有第一筆帶 entity_id；其餘欄位不完整的筆數直接略過
        eid = series[0].get('entity_id')
        rows = [x for x in series if isinstance(x, dict) and 'state' in x and safe_ts(x.get('last_changed')) is not None]
        if isinstance(eid, str) and rows:
            out[eid] = rows
    return out


def parse_ts(s):
    return datetime.datetime.fromisoformat(s.replace('Z', '+00:00'))


def safe_ts(s):
    """時間字串 → datetime；格式不對或不是字串回 None（HA 回應欄位不可信，不讓它變成 500）。"""
    if not isinstance(s, str):
        return None
    try:
        return parse_ts(s)
    except ValueError:
        return None
