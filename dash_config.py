"""設定載入。

所有可調整的項目都集中在一個 JSON 設定檔（預設是專案資料夾裡的 config.json，
可用環境變數 HOME_DASH_CONFIG 指到別處）。範例與每個欄位的說明見 config.example.json。

原則：
  * 沒有設定檔也能啟動（所有功能都會顯示「尚未設定」，不會崩潰）。
  * 金鑰、權杖不寫在設定檔裡：Home Assistant 權杖走環境變數或權限 600 的檔案。
  * 設定檔裡開頭是底線的鍵（例如 "_說明"）一律當註解忽略。
"""
import copy
import json
import os
import re
import stat
import sys
import urllib.parse

import dash_cardspec

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get('HOME_DASH_CONFIG') or os.path.join(HERE, 'config.json')

DEFAULTS = {
    'title': '家用儀表板',
    'server': {
        'host': '127.0.0.1',              # 只給本機看。要讓同網路的手機看，改成 0.0.0.0 並務必設 access_token
        'port': 9877,
        'allowed_networks': ['127.0.0.1/32', '::1/128'],   # 允許連線的來源網段（CIDR）
        'allowed_hosts': [],              # 用「主機名稱」（不是 IP）連進來時，名稱要列在這裡；IP 與 localhost 一律允許
        'access_token': '',               # 非空時，瀏覽器要先在 /login 頁面輸入一次密語（存 cookie）；非本機綁定時必填
        'tls_cert': '',                   # 選填：自帶 HTTPS 的憑證與私鑰（PEM）。兩個都填才啟用
        'tls_key': '',
        'public_https': False,            # 前面有負責 HTTPS 的反向代理、本程式自己是 HTTP 時設 true（cookie 加 Secure）
    },
    'utc_offset_hours': 8,                # 顯示與「今天」的判斷用的時區（台灣是 +8）
    'data_dir': 'data',                   # 所有執行時資料（軌跡、美食庫…）放這裡，相對路徑以專案資料夾為準
    'features': {                         # 全部可以個別關掉；關掉的功能整個分頁／卡片都不會出現
        'commute': True,
        'food': True,
        'custom': True,
        'metro': True,
    },
    'ha': {                               # Home Assistant（通勤軌跡的定位收集器與「自訂」分頁需要）
        'url': '',                        # 例：http://homeassistant.local:8123
        'token_env': 'HA_TOKEN',          # 長期存取權杖放在這個環境變數
        'token_file': '',                 # 或放在這個檔案（建議權限 600）；環境變數優先
        'verify_ssl': True,
        'ca_file': '',                    # 自簽憑證時填 CA 檔路徑
        'timeout_seconds': 20,            # 連線逾時（3～60）
    },
    'map': {
        'google_maps_js_key': '',         # 選填。有的話地圖多 Google 底圖；沒有就用 OpenStreetMap
        'center': [25.0478, 121.5170],    # 地圖預設中心 [緯度, 經度]（沒有軌跡時用）
    },
    'places': {},                         # 常用地點：名稱 -> {"lat":, "lon":, "radius": 公尺, "icon": "🏠"}
    'commute': {
        'entities': [],                   # HA 的 device_tracker 實體（手機）。第一個是主要來源
        'max_accuracy_m': 60,             # 定位誤差大於這個值的點不採用
        'stay_min_minutes': 8,            # 停留多久才算「停留」
        'stay_radius_m': 50,              # 連續點都在這個半徑內才算同一個停留
        'retention_days': 365,            # 收集器保留幾天的軌跡
    },
    'food': {
        'db': 'food.sqlite',              # 相對 data_dir
        'price_tiers': [400, 1000],       # 價位分層：低於第一個＝平價，到第二個之前＝吃好一點，以上＝高價
        'drink_categories': ['飲料', '咖啡', '甜點'],   # 骰子預設排除的類別（勾「喝的／甜點」才納入）
        'near_radius_max_m': 1500,
        'gps_max_age_minutes': 20,        # 「跟著手機」只採用幾分鐘內的定位
    },
    'metro': {
        'credentials_file': '',           # 臺北捷運 API 帳密檔（JSON：{"account": "…", "password": "…"}，權限 600）。環境變數優先
        'account_env': 'METRO_ACCOUNT',   # 或用這兩個環境變數提供（兩個都有才採用）
        'password_env': 'METRO_PASSWORD',
        'stations': [],                   # 常用站（站名，例：["台北車站"]，最多 10 個）
        'nearby_stations': 2,             # 車廂擁擠度：顯示常用站前後各幾站內的列車
        'cache_seconds': 30,              # 伺服器端快取秒數（不得小於 30，避免對捷運伺服器造成負擔）
        'timeout_seconds': 15,
        'max_response_bytes': 5000000,    # 回應大小上限
        'api_base': 'https://api.metro.taipei/metroapi',     # 一般不用改（測試用假伺服器才會改）
        'station_list_url': 'https://ws.metro.taipei/trtcBeaconBE/RouteControl.asmx',
    },
    'custom': {                           # 「自訂」分頁：用設定檔宣告唯讀的卡片，資料來自你的 Home Assistant（說明書：docs/自訂分頁說明書.md）
        'title': '自訂',                  # 分頁按鈕的名稱
        'cache_seconds': 30,              # 伺服器端快取秒數（10～3600）
        'cards': [],                      # 卡片清單；格式與範例見說明書
    },
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if str(k).startswith('_'):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != 'places':
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _strip_comments(obj):
    if isinstance(obj, dict):
        return {k: _strip_comments(v) for k, v in obj.items() if not str(k).startswith('_')}
    if isinstance(obj, list):
        return [_strip_comments(x) for x in obj]
    return obj


class ConfigError(Exception):
    pass


TOKEN_RE = re.compile(r'[A-Za-z0-9._~-]{12,128}')
TOKEN_HINT = ('只能用英文字母、數字與 - . _ ~，長度 12–128（中文、空白、引號、分號、換行都不行，因為密語要放進 HTTP 標頭）。'
              '可以這樣產生一個：python3 -c "import secrets;print(secrets.token_urlsafe(24))"')


def _check_token(value, name):
    if value in (None, ''):
        return
    if not isinstance(value, str) or not TOKEN_RE.fullmatch(value):
        raise ConfigError(f'{name} {TOKEN_HINT}')


def load(path=None):
    path = path or CONFIG_PATH
    try:
        with open(path, encoding='utf-8') as f:
            raw = json.load(f)
    except FileNotFoundError:
        return copy.deepcopy(DEFAULTS)
    except ValueError as e:
        raise ConfigError(f'設定檔 {path} 不是合法的 JSON：{e}') from None
    if not isinstance(raw, dict):
        raise ConfigError(f'設定檔 {path} 最外層必須是物件')
    cfg = _merge(DEFAULTS, _strip_comments(raw))
    _check(cfg)
    return cfg


def _check(cfg):
    port = cfg['server'].get('port')
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError('server.port 要是 1–65535 的整數')
    if not isinstance(cfg['server'].get('allowed_networks'), list):
        raise ConfigError('server.allowed_networks 要是清單')
    hosts = cfg['server'].get('allowed_hosts')
    if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
        raise ConfigError('server.allowed_hosts 要是主機名稱字串的清單')
    _check_token(cfg['server'].get('access_token'), 'server.access_token')
    if bool(cfg['server'].get('tls_cert')) != bool(cfg['server'].get('tls_key')):
        raise ConfigError('server.tls_cert 與 server.tls_key 要一起填')
    off = cfg.get('utc_offset_hours')
    if isinstance(off, bool) or not isinstance(off, (int, float)) or not -12 <= off <= 14:
        raise ConfigError('utc_offset_hours 要是 -12 到 14 的數字')
    for name, p in cfg['places'].items():
        if not isinstance(p, dict) or not _is_num(p.get('lat')) or not _is_num(p.get('lon')):
            raise ConfigError(f'places.{name} 要有數字的 lat 與 lon')
        if not (-90 <= p['lat'] <= 90 and -180 <= p['lon'] <= 180):
            raise ConfigError(f'places.{name} 的 lat 要在 -90～90、lon 要在 -180～180')
        if 'radius' in p and not (_is_num(p['radius']) and 0 < p['radius'] <= 100000):
            raise ConfigError(f'places.{name}.radius 要是 0 到 100000 之間的數字（公尺）')
        if 'icon' in p and not (isinstance(p['icon'], str) and len(p['icon']) <= 16):
            raise ConfigError(f'places.{name}.icon 要是不超過 16 個字的文字（例如一個 emoji）')
    tiers = cfg['food'].get('price_tiers')
    if not (isinstance(tiers, list) and len(tiers) == 2 and all(_is_num(x) for x in tiers) and tiers[0] < tiers[1]):
        raise ConfigError('food.price_tiers 要是兩個由小到大的數字')
    if not isinstance(cfg['commute'].get('entities'), list):
        raise ConfigError('commute.entities 要是清單')
    _check_metro(cfg['metro'])
    _check_custom(cfg['custom'])
    _check_ha(cfg['ha'])


def _check_ha(h):
    t = h.get('timeout_seconds')
    if not _is_num(t) or not 3 <= t <= 60:
        raise ConfigError('ha.timeout_seconds 要在 3～60 之間')


def _check_custom(c):
    if not isinstance(c.get('title'), str) or not 1 <= len(c['title']) <= 12:
        raise ConfigError('custom.title 要是 1～12 個字的文字（分頁按鈕的名稱）')
    cs = c.get('cache_seconds')
    if not _is_num(cs) or not 10 <= cs <= 3600:
        raise ConfigError('custom.cache_seconds 要在 10～3600 之間')
    try:
        c['cards'] = dash_cardspec.validate(c.get('cards'))
    except dash_cardspec.CardError as e:
        raise ConfigError(str(e)) from None


def _check_metro(m):
    st = m.get('stations')
    if not (isinstance(st, list) and len(st) <= 10 and all(isinstance(x, str) and 0 < len(x.strip()) <= 20 for x in st)):
        raise ConfigError('metro.stations 要是站名字串的清單（最多 10 個、每個 1～20 字）')
    n = m.get('nearby_stations')
    if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= 6:
        raise ConfigError('metro.nearby_stations 要是 0～6 的整數')
    c = m.get('cache_seconds')
    if not _is_num(c) or not 30 <= c <= 3600:
        raise ConfigError('metro.cache_seconds 要在 30～3600 之間（不得小於 30 秒，避免對捷運伺服器造成負擔）')
    t = m.get('timeout_seconds')
    if not _is_num(t) or not 3 <= t <= 60:
        raise ConfigError('metro.timeout_seconds 要在 3～60 之間')
    b = m.get('max_response_bytes')
    if isinstance(b, bool) or not isinstance(b, int) or not 100000 <= b <= 20000000:
        raise ConfigError('metro.max_response_bytes 要在 100000～20000000 之間')
    for key in ('api_base', 'station_list_url'):
        u = urllib.parse.urlsplit(str(m.get(key) or ''))
        if not (u.scheme == 'https' or (u.scheme == 'http' and u.hostname in ('127.0.0.1', 'localhost', '::1'))) or not u.hostname:
            raise ConfigError(f'metro.{key} 必須是 https 網址（只有本機測試可用 http），以免帳密以明文傳送')
    for key in ('account_env', 'password_env'):
        if not isinstance(m.get(key), str):
            raise ConfigError(f'metro.{key} 要是環境變數名稱字串')
    if not isinstance(m.get('credentials_file'), str):
        raise ConfigError('metro.credentials_file 要是路徑字串')


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


CONFIG_ERROR = None
try:
    CFG = load()
except ConfigError as _e:
    # 設定檔壞掉時，import 本模組不能丟 traceback：先退回預設值並記下原因，
    # 由 server.main() 與 tools/ 的進入點呼叫 exit_if_invalid() 用中文訊息結束。
    CONFIG_ERROR = str(_e)
    CFG = copy.deepcopy(DEFAULTS)


def exit_if_invalid():
    """進入點（server.main、tools/*）開頭呼叫：設定檔有問題就印中文訊息並以非 0 結束，不顯示 traceback。"""
    if CONFIG_ERROR:
        raise SystemExit(f'設定檔有問題：{CONFIG_ERROR}')


def reload(path=None):
    """換一份設定（測試、--config 用）。壞掉時丟 ConfigError，原本的設定不變。"""
    global CFG, CONFIG_ERROR
    cfg = load(path)
    CFG, CONFIG_ERROR = cfg, None
    return CFG


def data_path(*parts):
    base = CFG['data_dir']
    if not os.path.isabs(base):
        base = os.path.join(HERE, base)
    return os.path.join(base, *parts)


def ensure_data_dir():
    """建立資料夾（權限 700）。資料夾若是使用者自己先建的、權限比 700 寬，印一次警告（不擅自改權限）。"""
    d = data_path()
    os.makedirs(d, mode=0o700, exist_ok=True)
    try:
        if os.stat(d).st_mode & 0o077 and d not in _warned_dirs:
            _warned_dirs.add(d)
            print(f'警告：資料夾 {d} 的權限比 700 寬，同一台機器的其他使用者可能讀得到你的軌跡與財務資料；'
                  f'建議執行 chmod 700 {d}', file=sys.stderr)
    except OSError:
        pass


_warned_dirs = set()


def _secure_read(path, label):
    """讀祕密檔：權限太鬆（群組／其他人可讀）或讀不到就丟 ConfigError；檔案不存在回 None。"""
    path = os.path.expanduser(path)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ConfigError(f'讀不到{label} {path}：{e.strerror}') from None
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(f'{label} {path} 權限太鬆，請執行：chmod 600 {path}')
    try:
        with open(path, encoding='utf-8') as f:
            return f.read()
    except OSError as e:
        raise ConfigError(f'讀不到{label} {path}：{e.strerror}') from None


def ha_token():
    """回傳 HA 權杖字串，沒設定回空字串。權杖檔權限太鬆（群組／其他人可讀）或讀不到時丟 ConfigError。"""
    env = CFG['ha'].get('token_env')
    if env and os.environ.get(env):
        return os.environ[env].strip()
    tf = CFG['ha'].get('token_file')
    if tf:
        text = _secure_read(tf, '權杖檔')
        return text.strip() if text is not None else ''
    return ''


def _cred_ok(v):
    return isinstance(v, str) and 0 < len(v) <= 200 and all(c.isprintable() for c in v)


def metro_credentials():
    """臺北捷運 API 的 (帳號, 密碼)；沒設定回 ('', '')。環境變數（兩個都有）優先，其次權限 600 的 JSON 檔。
    內容有控制字元、太長、檔案權限太鬆或格式不對時丟 ConfigError。訊息裡絕不含帳密內容。"""
    m = CFG['metro']
    acc, pw = os.environ.get(m.get('account_env') or '', ''), os.environ.get(m.get('password_env') or '', '')
    if acc and pw:
        if not (_cred_ok(acc) and _cred_ok(pw)):
            raise ConfigError('捷運帳號或密碼環境變數的內容不合格（不可有控制字元、不可超過 200 字）')
        return acc, pw
    cf = m.get('credentials_file')
    if cf:
        text = _secure_read(cf, '捷運帳密檔')
        if text is None:
            return '', ''
        try:
            d = json.loads(text)
        except ValueError:
            raise ConfigError(f'捷運帳密檔 {cf} 不是合法的 JSON（格式：{{"account": "…", "password": "…"}}）') from None
        if not isinstance(d, dict) or not _cred_ok(d.get('account')) or not _cred_ok(d.get('password')):
            raise ConfigError(f'捷運帳密檔 {cf} 要有字串的 account 與 password（不可有控制字元、不可超過 200 字）')
        return d['account'], d['password']
    return '', ''
