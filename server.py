#!/usr/bin/env python3
"""家用儀表板：純標準庫 http.server ＋ 原生 JS 前端，沒有建置步驟，也不需要安裝任何套件。

  python3 server.py                     # 照 config.json 的 server.host／port 啟動
  python3 server.py --port 19877        # 臨時換埠
  python3 server.py --config /path/to/other.json

功能（每一項都可以在設定檔 features 關掉，沒設定好時只會顯示「尚未設定」，不會讓伺服器出錯）：
  commute 通勤軌跡　food 美食庫（骰子／附近／瀏覽）　custom 自訂分頁（設定檔宣告的唯讀 Home Assistant 卡片）
  metro 臺北捷運（列車到站倒數、車廂擁擠度）
"""
import argparse
import hmac
import json
import os
import ssl
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import dash_access
import dash_commute
import dash_config as C
import dash_food
import dash_geo as G
import dash_ha
import dash_custom
import dash_metro
import dash_validate as V
from dash_support import APIError, NotConfigured

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, 'static')
STATIC_FILES = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/app.js': ('app.js', 'application/javascript; charset=utf-8'),
    '/app.css': ('app.css', 'text/css; charset=utf-8'),
    '/favicon.svg': ('favicon.svg', 'image/svg+xml'),
}
ACCESS = None   # 啟動時依設定建立（測試可自己指定）


def feature_status():
    """每個功能：enabled（設定檔開關）、ready（資料／設定是否到位）、reason（沒到位的原因）。"""
    cfg, f = C.CFG, C.CFG['features']
    exists = lambda name: os.path.exists(C.data_path(name))
    st = {}

    def put(key, ready, reason=''):
        st[key] = {'enabled': bool(f.get(key)), 'ready': bool(ready), 'reason': '' if ready else reason}

    if cfg['commute']['entities']:
        put('commute', exists('locations.sqlite'), '還沒有軌跡資料：請先啟動 tools/loc_collect.py（見 README）')
    else:
        put('commute', False, '尚未設定 commute.entities（Home Assistant 的 device_tracker）')
    put('food', exists(cfg['food']['db']), '還沒有美食庫：請先用 tools/food_import.py 匯入店家 CSV')
    put('custom', not dash_custom.configured_reason(), dash_custom.configured_reason())
    put('metro', not dash_metro.configured_reason(), dash_metro.configured_reason())
    return st


def api_features(q):
    return {'title': C.CFG['title'], 'features': feature_status(), 'ha_configured': dash_ha.configured(),
            'map': {'center': C.CFG['map']['center'], 'google': bool(C.CFG['map']['google_maps_js_key'])},
            'places': sorted(C.CFG['places']), 'today': G.today(), 'custom_title': C.CFG['custom']['title'],
            'tiers': C.CFG['food']['price_tiers']}


def api_mapkey(q):
    k = C.CFG['map']['google_maps_js_key']
    if not k:
        raise APIError('沒有設定 Google 地圖金鑰', 404, 'no_key')
    return {'key': k}


def api_track(q):
    date = V.date(q.get('date') or G.today(), '日期')
    return dash_commute.track(date, nearest_shop=dash_food.nearest_shop)


# ── 路由表：路徑 → (功能鍵, 處理函式)。POST 的處理函式收到 (body dict)。 ──
GET_ROUTES = {
    '/api/mapkey': (None, api_mapkey),
    '/api/track': ('commute', api_track),
    '/api/food/stats': ('food', lambda q: dash_food.stats()),
    '/api/food/districts': ('food', lambda q: dash_food.districts()),
    '/api/food/cats': ('food', dash_food.cats),
    '/api/food/list': ('food', dash_food.listing),
    '/api/food/dice': ('food', dash_food.dice),
    '/api/food/near': ('food', dash_food.near),
    '/api/food/ate': ('food', lambda q: dash_food.ate()),
    '/api/custom': ('custom', lambda q: dash_custom.render()),
    '/api/metro/arrivals': ('metro', lambda q: dash_metro.arrivals()),
    '/api/metro/crowding': ('metro', lambda q: dash_metro.crowding()),
}


POST_ROUTES = {
    '/api/food/tried': ('food', dash_food.mark_tried),
}


LOGIN_BACKOFF_BASE = 1.0   # 同一來源位址連續登入失敗後的鎖定秒數基數：第 n 次失敗後鎖 基數×2^(n-1) 秒（最多 60 秒）；0＝不限制（測試用）
LOGIN_BACKOFF_MAX = 60.0
_login_fails = {}          # 來源位址 -> [連續失敗次數, 鎖定到的時間, 最後一次失敗的時間]
_login_lock = threading.Lock()


def login_gate(ip, ok_fn, now=None):
    """登入的暴力破解防護（以來源位址計）。回傳 ('ok'|'bad'|'locked', 剩餘鎖定秒數)。
    鎖定期間連密語都不檢查，直接回 locked（不佔執行緒睡覺），所以並行猜測也沒辦法加快速度；成功登入會清掉該來源的紀錄。"""
    now = time.time() if now is None else now
    with _login_lock:
        for k in [k for k, v in _login_fails.items() if now - v[2] > 600]:     # 10 分鐘沒動靜的紀錄丟掉
            del _login_fails[k]
        if len(_login_fails) > 1000:
            _login_fails.pop(min(_login_fails, key=lambda k: _login_fails[k][2]))
        rec = _login_fails.get(ip)
        if rec and rec[1] > now:
            return 'locked', rec[1] - now
        if ok_fn():
            _login_fails.pop(ip, None)
            return 'ok', 0
        n = (rec[0] if rec else 0) + 1
        wait = min(LOGIN_BACKOFF_BASE * 2 ** (n - 1), LOGIN_BACKOFF_MAX) if LOGIN_BACKOFF_BASE else 0
        _login_fails[ip] = [n, now + wait, now]
        return 'bad', wait
LOG_REQUESTS = True      # 只記 來源位址、方法、路徑（不含 ? 之後的內容）、狀態碼；測試時會關掉

LOGIN_PAGE = """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>登入</title><style>body{font-family:-apple-system,"Noto Sans TC",sans-serif;background:#f5f6f8;display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
form{background:#fff;padding:24px;border-radius:12px;box-shadow:0 1px 6px rgba(0,0,0,.12);width:min(92vw,340px)}h1{font-size:18px;margin:0 0 14px;color:#3b4a6b}
input{width:100%;padding:9px;font-size:15px;border:1px solid #cfd5e3;border-radius:6px;box-sizing:border-box}button{margin-top:12px;width:100%;padding:9px;font-size:15px;border:0;border-radius:6px;background:#3b4a6b;color:#fff;cursor:pointer}
.err{color:#a33;font-size:13px;margin:0 0 10px}.hint{color:#777;font-size:12px;margin-top:10px}</style></head><body>
<form method="post" action="/login"><h1>__TITLE__</h1>__ERROR__<input type="password" name="token" placeholder="存取密語" autocomplete="current-password" autofocus required>
<button type="submit">登入</button><div class="hint">密語在設定檔的 server.access_token。</div></form></body></html>"""


def csp_headers():
    """內容安全政策。沒設 Google 金鑰時整份強制執行；有設時只強制不影響 Google 地圖的那幾條，
    完整政策以「僅回報」模式送出（Google 地圖會動態載入多個網域與 worker，硬擋容易讓底圖壞掉）。"""
    base = "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"
    strict = ("default-src 'none'; script-src 'self' https://unpkg.com; style-src 'self' 'unsafe-inline' https://unpkg.com; "
              "img-src 'self' data: blob: https://tile.openstreetmap.org https://unpkg.com; connect-src 'self'; " + base)
    if not C.CFG['map']['google_maps_js_key']:
        return {'Content-Security-Policy': strict}
    loose = ("default-src 'none'; script-src 'self' https://unpkg.com https://*.googleapis.com https://*.gstatic.com; "
             "style-src 'self' 'unsafe-inline' https://unpkg.com https://*.googleapis.com; "
             "img-src 'self' data: blob: https://tile.openstreetmap.org https://unpkg.com https://*.googleapis.com https://*.gstatic.com https://*.google.com https://*.ggpht.com; "
             "connect-src 'self' https://*.googleapis.com; worker-src blob:; " + base)
    return {'Content-Security-Policy': base, 'Content-Security-Policy-Report-Only': loose}


class Handler(BaseHTTPRequestHandler):
    server_version = 'HomeDash'
    timeout = 30      # 單一連線讀取逾時（秒）：半開或不送本文的請求不會永久佔住執行緒

    def log_message(self, *a):
        pass

    def log_request(self, code='-', size='-'):
        if LOG_REQUESTS:
            # 路徑可能含控制字元（終端機逃脫序列）：一律跳脫成 \x1b 這種可見形式再寫進日誌
            path = urllib.parse.urlparse(self.path).path.encode('unicode_escape').decode('ascii')
            sys.stderr.write('%s %s %s %s\n' % (self.client_address[0], self.command, path, code))

    # ── 輸出 ──
    def send(self, body, ctype='application/json; charset=utf-8', code=200, headers=None):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        # 用 same-origin 而不是 no-referrer：後者會讓瀏覽器在表單 POST（登入）時送出 Origin: null，同源檢查就誤擋。
        # same-origin 一樣不會把網址帶給 unpkg／OSM／Google 等第三方。
        self.send_header('Referrer-Policy', 'same-origin')
        for k, v in csp_headers().items():
            self.send_header(k, v)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(b)

    def send_json(self, obj, code=200):
        self.send(json.dumps(obj, ensure_ascii=False), code=code)

    def fail(self, message, status, **extra):
        self.send_json(dict({'error': message}, **extra), status)

    def login_page(self, error='', code=200):
        from html import escape
        err = f'<p class="err">{escape(error)}</p>' if error else ''
        self.send(LOGIN_PAGE.replace('__TITLE__', escape(C.CFG['title'])).replace('__ERROR__', err), 'text/html; charset=utf-8', code)

    # ── 存取檢查 ──
    def authorized(self, u, post=False):
        if not ACCESS.ip_ok(self.client_address[0]):
            self.fail('這個來源位址不在允許名單內（設定檔 server.allowed_networks）', 403)
            return False
        if not ACCESS.host_ok(self.headers):
            self.fail('Host 標頭不在允許名單內：用主機名稱連線時，請把名稱加進設定檔 server.allowed_hosts', 403)
            return False
        if post and not ACCESS.same_origin(self.headers):
            self.fail('跨站請求被拒絕（Origin 的協定、主機、埠必須與網址一致）', 403)
            return False
        if u.path == '/login':
            return True       # 登入頁本身不需要密語
        if not ACCESS.token_ok(self.headers):
            if self.command in ('GET', 'HEAD') and not u.path.startswith('/api/'):
                self.send('', 'text/plain', 302, {'Location': '/login'})
            else:
                self.fail('需要登入：請先用瀏覽器開啟儀表板並輸入存取密語', 401)
            return False
        return True

    def do_login(self):
        if not ACCESS.token:
            return self.send('', 'text/plain', 302, {'Location': '/'})
        n = V.content_length(self.headers, 4096, '登入表單')
        raw = self.rfile.read(n) if n else b''
        ctype = (self.headers.get('Content-Type') or '').split(';')[0].strip()
        if ctype == 'application/json':
            given = V.json_body(raw).get('token', '')
        else:
            given = (urllib.parse.parse_qs(raw.decode('utf-8', 'replace')).get('token') or [''])[0]
        state, wait = login_gate(self.client_address[0], lambda: ACCESS.check_login(given))
        if state == 'ok':
            return self.send('', 'text/plain', 303, {'Location': '/', 'Set-Cookie': ACCESS.cookie_header()})
        if state == 'locked':
            retry = str(int(wait) + 1)
            msg = f'失敗次數太多，請 {retry} 秒後再試'
            if ctype == 'application/json':
                return self.send(json.dumps({'error': msg}, ensure_ascii=False), code=429, headers={'Retry-After': retry})
            from html import escape
            err = f'<p class="err">{escape(msg)}</p>'
            return self.send(LOGIN_PAGE.replace('__TITLE__', escape(C.CFG['title'])).replace('__ERROR__', err),
                             'text/html; charset=utf-8', 429, {'Retry-After': retry})
        if ctype == 'application/json':
            return self.fail('密語不正確', 401)
        self.login_page('密語不正確', 401)

    # ── 方法 ──
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if not self.authorized(u):
            return
        try:
            if u.path == '/login':
                if not ACCESS.token or ACCESS.token_ok(self.headers):
                    return self.send('', 'text/plain', 302, {'Location': '/'})
                return self.login_page()
            if u.path in STATIC_FILES:
                name, ctype = STATIC_FILES[u.path]
                try:
                    with open(os.path.join(STATIC, name), 'rb') as f:
                        data = f.read()
                except FileNotFoundError:       # 只有靜態檔不存在才是 404；其他地方的 FileNotFoundError 是設定問題，不能偽裝成「找不到」
                    return self.fail('找不到', 404)
                return self.send(data, ctype)
            if u.path == '/favicon.ico':
                return self.send('', 'text/plain', 204)
            if u.path == '/api/features':
                return self.send_json(api_features({}))
            route = GET_ROUTES.get(u.path)
            if not route:
                return self.fail('找不到', 404)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            self.run_route(route, q)
        except Exception as e:   # noqa: BLE001
            self.internal_error(e)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if not self.authorized(u, post=True):
            return
        try:
            if u.path == '/login':
                return self.do_login()
            n = V.content_length(self.headers, V.MAX_JSON, 'JSON 本文')
            route = POST_ROUTES.get(u.path)
            if not route:
                return self.fail('找不到', 404)
            body = V.json_body(self.rfile.read(n)) if n else {}
            self.run_route(route, body)
        except V.Invalid as e:   # 標頭或 JSON 本文不合法
            self.fail(str(e), 400)
        except Exception as e:   # noqa: BLE001
            self.internal_error(e)

    def run_route(self, route, arg):
        feature, fn = route
        if feature and not C.CFG['features'].get(feature):
            return self.fail('這個功能已在設定檔停用', 404, disabled=True)
        try:
            self.send_json(fn(arg))
        except NotConfigured as e:
            self.send_json({'configured': False, 'reason': e.reason})
        except V.Invalid as e:
            self.fail(str(e), 400)
        except APIError as e:
            self.fail(str(e), e.status, code=e.code)
        except ValueError as e:    # 資料檔壞掉等「使用者可自行處理」的問題
            self.fail(str(e), 422)

    def internal_error(self, e):
        traceback.print_exc(file=sys.stderr)
        try:
            self.fail(f'伺服器內部錯誤（{type(e).__name__}），詳情請看伺服器的終端機輸出', 500)
        except OSError:
            pass


LOOPBACK = ('127.0.0.1', 'localhost', '::1')


def check_startup(host):
    """啟動前的安全檢查：綁在非本機位址卻沒有存取密語就拒絕。"""
    if host not in LOOPBACK and not host.startswith('127.') and not C.CFG['server']['access_token']:
        raise C.ConfigError(f'server.host 是 {host}（不只本機能連），但沒有設定 server.access_token。'
                            '請先設定存取密語（可用 python3 -c "import secrets;print(secrets.token_urlsafe(24))" 產生），或把 host 改回 127.0.0.1')


def make_server(host, port):
    global ACCESS
    check_startup(host)
    ACCESS = dash_access.Access(C.CFG['server'])
    httpd = ThreadingHTTPServer((host, port), Handler)
    s = C.CFG['server']
    if s['tls_cert']:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            ctx.load_cert_chain(os.path.expanduser(s['tls_cert']), os.path.expanduser(s['tls_key']))
        except (OSError, ssl.SSLError) as e:
            httpd.server_close()
            raise C.ConfigError(f'讀不到或不合法的 TLS 憑證／私鑰（server.tls_cert、tls_key）：{e}') from None
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    return httpd


def main(argv=None):
    ap = argparse.ArgumentParser(description='家用儀表板')
    ap.add_argument('--config', help='設定檔路徑（預設 config.json 或環境變數 HOME_DASH_CONFIG）')
    ap.add_argument('--host')
    ap.add_argument('--port', type=int)
    a = ap.parse_args(argv)
    try:
        C.reload(a.config or C.CONFIG_PATH)
    except C.ConfigError as e:
        sys.exit(f'設定檔有問題：{e}')
    s = C.CFG['server']
    host, port = a.host or s['host'], a.port or s['port']
    try:
        httpd = make_server(host, port)
    except C.ConfigError as e:
        sys.exit(f'無法啟動：{e}')
    except OSError as e:
        sys.exit(f'無法在 {host}:{port} 啟動：{e}')
    scheme = 'https' if s['tls_cert'] else 'http'
    if host not in LOOPBACK and not host.startswith('127.') and not (s['tls_cert'] or s['public_https']):
        print('警告：目前是純 HTTP 且不只本機能連，存取密語與 cookie 會以明文在網路上傳送。'
              '跨機器使用請設定 server.tls_cert／tls_key、或放在 HTTPS 反向代理後（並設 server.public_https）、或走 VPN 等加密通道。', file=sys.stderr)
    print(f'{C.CFG["title"]} 啟動：{scheme}://{host}:{port}/', flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止')


if __name__ == '__main__':
    main()
