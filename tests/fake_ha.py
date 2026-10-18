"""測試用的假 Home Assistant：只實作儀表板會讀的三個 GET 端點。"""
import datetime
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = 'test-token'


class FakeHA:
    def __init__(self, states, history, raw=None):
        """states: {entity_id: {'state':…, 'attributes':{…}, 'last_changed':…, 'last_updated':…}}
        history: {entity_id: [(iso_time, state), …]}"""
        self.states, self.hist = states, history
        self.raw = raw or {}
        self.requests = []       # 收到的請求路徑（含查詢字串），測試用來數次數
        self.delay = 0           # 每個請求先睡幾秒（模擬逾時）     # 路徑前綴 -> 直接回這個 JSON（用來模擬 HA 回傳怪形狀）
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.headers.get('Authorization') != 'Bearer ' + TOKEN:
                    self.send_response(401)
                    self.end_headers()
                    return
                outer.requests.append(self.path)
                if outer.delay:
                    time.sleep(outer.delay)
                path = self.path.split('?')[0]
                override = next((v for k, v in outer.raw.items() if path.startswith(k)), None)
                if override is not None:
                    body = override
                elif path == '/api/states':
                    body = [dict(entity_id=k, **v) for k, v in outer.states.items()]
                elif path.startswith('/api/states/'):
                    ent = path[len('/api/states/'):]
                    if ent not in outer.states:
                        self.send_response(404)
                        self.end_headers()
                        return
                    body = dict(entity_id=ent, **outer.states[ent])
                elif path.startswith('/api/history/period/'):
                    q = self.path.split('filter_entity_id=')[1].split('&')[0]
                    ents = [e for e in urllib.parse.unquote(q).split(',') if e in outer.hist]
                    body = []
                    for e in ents:
                        rows = [{'state': s, 'last_changed': t} for t, s in outer.hist[e]]
                        rows[0]['entity_id'] = e
                        body.append(rows)
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat()
