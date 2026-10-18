"""存取控制：來源網段白名單、Host 白名單、可選的存取密語（登入頁＋cookie）、寫入請求的同源檢查。

預設只允許本機（127.0.0.1）連線。要讓同一個區域網路的手機看：
  1. server.host 改成 0.0.0.0（或這台機器的區網位址）
  2. server.allowed_networks 加上你的區網網段，例如 "192.168.1.0/24"
  3. 一定要設 server.access_token（沒設的話，非本機綁定會直接拒絕啟動）
  4. 用主機名稱（不是 IP）連的話，名稱要列進 server.allowed_hosts
  5. 密語與 cookie 在網路上是明文傳輸，跨機器請用 HTTPS（server.tls_cert／tls_key，或前面放反向代理）
     或 VPN 等加密通道；不要在不受信任的網路用純 HTTP。

不信任任何 X-Forwarded-* 標頭（只看實際的 TCP 來源位址），所以放在反向代理後面時，
請讓代理與本程式在同一台機器上，並把代理的位址（通常是 127.0.0.1）放進 allowed_networks，
同時在代理上做好登入驗證。
"""
import hashlib
import hmac
import http.cookies
import ipaddress
import time
import urllib.parse

COOKIE = 'dash_token'
SESSION_SECONDS = 30 * 86400      # 登入 cookie 的伺服器端有效期（過期要重新輸入密語）


class Access:
    def __init__(self, server_cfg):
        self.nets = []
        for n in server_cfg.get('allowed_networks') or []:
            try:
                self.nets.append(ipaddress.ip_network(str(n), strict=False))
            except ValueError:
                raise ValueError(f'server.allowed_networks 裡的 {n!r} 不是合法的網段') from None
        self.hosts = {str(h).strip().lower() for h in (server_cfg.get('allowed_hosts') or []) if str(h).strip()}
        self.token = str(server_cfg.get('access_token') or '')
        self.https = bool(server_cfg.get('tls_cert') or server_cfg.get('public_https'))
        self.scheme = 'https' if self.https else 'http'

    # ── 來源與 Host ──
    def ip_ok(self, ip):
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if addr.version == 6 and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        return any(addr in n for n in self.nets)

    def host_ok(self, headers):
        """擋 DNS 重綁：Host 只接受 IP 字面值、localhost，或 server.allowed_hosts 裡的名稱。沒帶 Host 一律拒絕。"""
        raw = headers.get('Host', '')
        if not raw:
            return False
        try:
            name = (urllib.parse.urlsplit('//' + raw).hostname or '').lower()
        except ValueError:
            return False
        if not name:
            return False
        if name == 'localhost' or name in self.hosts:
            return True
        try:
            ipaddress.ip_address(name)    # 用 IP 連進來：DNS 騙不了 IP
            return True
        except ValueError:
            return False

    # ── 密語與 cookie ──
    def _sign(self, issued):
        return hmac.new(self.token.encode(), f'home-dash-session-v2:{issued}'.encode(), hashlib.sha256).hexdigest()

    def new_session(self, now=None):
        """cookie 值：<簽發時間>.<HMAC>。伺服器端只驗簽章與是否在 30 天內，不存狀態；
        改掉 access_token 就會讓所有已簽發的 cookie 立刻失效（目前沒有單獨撤銷某一個的功能）。"""
        issued = int(time.time() if now is None else now)
        return f'{issued}.{self._sign(issued)}'

    def session_ok(self, value, now=None):
        issued, _, sig = (value or '').partition('.')
        if not (issued.isascii() and issued.isdigit()) or len(issued) > 12 or not sig:
            return False
        if not hmac.compare_digest(sig.encode('utf-8', 'replace'), self._sign(int(issued)).encode()):
            return False
        age = (time.time() if now is None else now) - int(issued)
        return -60 <= age <= SESSION_SECONDS

    def token_ok(self, headers):
        if not self.token:
            return True
        given = headers.get('X-Dash-Token', '')
        if given and hmac.compare_digest(given.encode('utf-8', 'replace'), self.token.encode()):
            return True
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(headers.get('Cookie', ''))
        except http.cookies.CookieError:
            return False
        return COOKIE in jar and self.session_ok(jar[COOKIE].value)

    def check_login(self, given):
        """登入表單送來的密語是否正確。"""
        return bool(self.token) and bool(given) and hmac.compare_digest(str(given).encode('utf-8', 'replace'), self.token.encode())

    def cookie_header(self):
        parts = [f'{COOKIE}={self.new_session()}', 'Path=/', 'HttpOnly', 'SameSite=Strict', f'Max-Age={SESSION_SECONDS}']
        if self.https:
            parts.append('Secure')
        return '; '.join(parts)

    # ── 寫入請求的來源檢查 ──
    def same_origin(self, headers):
        """瀏覽器送出的寫入請求：Origin 的協定、主機、埠都要跟（已通過白名單的）Host 一致。
        沒有 Origin（curl、HA）放行；Sec-Fetch-Site 若有，只接受 same-origin／none。"""
        site = headers.get('Sec-Fetch-Site')
        if site and site not in ('same-origin', 'none'):
            return False
        origin = headers.get('Origin')
        if not origin:
            return True
        try:
            u = urllib.parse.urlsplit(origin)
        except ValueError:
            return False
        return u.scheme == self.scheme and u.netloc.lower() == headers.get('Host', '').lower() and not u.path.strip('/')
