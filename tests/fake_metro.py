"""測試用的假臺北捷運 API（SOAP）：記錄每個請求、可模擬各種壞回應。資料是編的，不連任何外網。"""
import datetime
import json
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GOOD_ACCOUNT = 'acct-for-tests'
GOOD_PASSWORD = 'pw<&>"\'-for-tests'      # 刻意含需要 XML 跳脫的字元

EMPTY_ENVELOPE = ('<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
                  '<soap:Body><xResponse xmlns="http://tempuri.org/" /></soap:Body></soap:Envelope>')


def soap_wrap(method, payload):
    return ('<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
            f'<soap:Body><{method}Response xmlns="http://tempuri.org/"><{method}Result>'
            + payload.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;') + f'</{method}Result></{method}Response></soap:Body></soap:Envelope>')


TRACK = [
    {'TrainNumber': '', 'StationName': '台北車站', 'DestinationName': '淡水站', 'CountDown': '06:42', 'NowDateTime': '-'},
    {'TrainNumber': '', 'StationName': '台北車站', 'DestinationName': '淡水站', 'CountDown': '01:05', 'NowDateTime': '-'},
    {'TrainNumber': '', 'StationName': '台北車站', 'DestinationName': '象山站', 'CountDown': '列車進站', 'NowDateTime': '-'},
    {'TrainNumber': '', 'StationName': '台北車站', 'DestinationName': '南港展覽館站', 'CountDown': '資料擷取中', 'NowDateTime': '-'},
    {'TrainNumber': '', 'StationName': '台北車站', 'DestinationName': '頂埔站', 'CountDown': '營運時間已過', 'NowDateTime': '-'},
    {'TrainNumber': '', 'StationName': '台北車站', 'DestinationName': '新店站', 'CountDown': '很奇怪的文字', 'NowDateTime': '-'},
    {'TrainNumber': '12', 'StationName': '板橋站', 'DestinationName': '松山站', 'CountDown': '00:39', 'NowDateTime': '-'},
    {'TrainNumber': '', 'StationName': '淡水站', 'DestinationName': '南港展覽館站', 'CountDown': '03:03', 'NowDateTime': '-'},
]
WEIGHT = [
    {'TrainNumber': '201', 'CN1': '165/166', 'CID': '1', 'StationID': 'R10', 'Cart1L': '1', 'Cart2L': '2', 'Cart3L': '3', 'Cart4L': '1', 'Cart5L': '9', 'Cart6L': '', 'utime': '-'},
    {'TrainNumber': '202', 'CN1': '161/162', 'CID': '2', 'StationID': 'R12', 'Cart1L': '1', 'Cart2L': '1', 'Cart3L': '1', 'Cart4L': '1', 'Cart5L': '1', 'Cart6L': '1', 'utime': '-'},
    {'TrainNumber': '203', 'CN1': '175/176', 'CID': '2', 'StationID': 'R20', 'Cart1L': '1', 'Cart2L': '1', 'Cart3L': '1', 'Cart4L': '1', 'Cart5L': '1', 'Cart6L': '1', 'utime': '-'},
    {'TrainNumber': '204', 'CN1': '105/106', 'CID': '1', 'StationID': 'BL12', 'Cart1L': '2', 'Cart2L': '2', 'Cart3L': '2', 'Cart4L': '2', 'Cart5L': '2', 'Cart6L': '2', 'utime': '-'},
    {'TrainNumber': '205', 'CN1': '107/108', 'CID': '1', 'StationID': '', 'Cart1L': '1', 'Cart2L': '1', 'Cart3L': '1', 'Cart4L': '1', 'Cart5L': '1', 'Cart6L': '1', 'utime': 'x'},
    'garbage-row',
]
BR_ROWS = [
    {'TrainNumber': '45,42', 'CID': '1', 'DU': '下行', 'StationID': 'BR24', 'StationName': 'whatever', 'CN1': '45', 'CN2': '42', 'Car1': '2', 'Car2': '1', 'Car3': '3', 'Car4': '1', 'UpdateTime': '-'},
    {'TrainNumber': '189,130', 'CID': '2', 'DU': '上行', 'StationID': 'BR16', 'Car1': '1', 'Car2': '1', 'Car3': '1', 'Car4': '1', 'UpdateTime': '-'},
]
STATIONS = [{'SID': '1', 'SCODE': 'R10', 'SCNAME': '台北車站'}, {'SID': '2', 'SCODE': 'BL12', 'SCNAME': '台北車站'},
            {'SID': '3', 'SCODE': 'R09', 'SCNAME': '台大醫院'}, {'SID': '4', 'SCODE': 'R11', 'SCNAME': '中山'},
            {'SID': '5', 'SCODE': 'R12', 'SCNAME': '忠孝復興'}, {'SID': '6', 'SCODE': 'R20', 'SCNAME': '石牌'},
            {'SID': '7', 'SCODE': 'BR24', 'SCNAME': '南港展覽館'}, {'SID': '8', 'SCODE': 'BL07', 'SCNAME': '板橋'},
            {'SID': '9', 'SCODE': 'Y17', 'SCNAME': '環狀線示範站'}, {'SID': '10', 'SCODE': 'BL07MALL', 'SCNAME': '中山地下街'},
            {'SID': '11', 'SCODE': 'BR16', 'SCNAME': '西湖'}]


TZ8 = datetime.timezone(datetime.timedelta(hours=8))
TIME_KEYS = ('NowDateTime', 'utime', 'UpdateTime')


def stamp(rows, age):
    """把資料列裡的時間欄位（值是 '-' 的）填成「現在往前 age 秒」的臺北時間；列裡可用 '_age' 個別指定。"""
    out = []
    for r in rows:
        if not isinstance(r, dict):
            out.append(r)
            continue
        r = dict(r)
        a = r.pop('_age', age)
        for k in TIME_KEYS:
            if r.get(k) == '-':
                r[k] = (datetime.datetime.now(TZ8) - datetime.timedelta(seconds=a)).strftime('%Y-%m-%d %H:%M:%S')
        out.append(r)
    return out


class FakeMetro:
    def __init__(self):
        self.requests = []          # (path, 帳號, 密碼, 本文 bytes)
        self.mode = {}              # path 結尾 -> 模式
        self.data = {'/TrackInfo.asmx': TRACK, '/CarWeight.asmx': WEIGHT, '/CarWeightBR.asmx': BR_ROWS, '/RouteControl.asmx': STATIONS}
        self.redirect_to = None
        self.fault_text = '帳號或密碼錯誤'
        self.result_text = '認證失敗：帳號或密碼錯誤'
        self.delay = 0
        self.delays = {}            # 路徑 -> 該路徑額外延遲秒數
        self.age = 5                # 資料時間距「現在」幾秒
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(n)
                path = self.path
                acct = pw = None
                try:
                    root = ET.fromstring(body)
                    for el in root.iter():
                        tag = el.tag.split('}')[-1]
                        if tag == 'userName':
                            acct = el.text
                        if tag == 'passWord':
                            pw = el.text
                except ET.ParseError:
                    pass
                outer.requests.append((path, acct, pw, body))
                mode = outer.mode.get(path, 'raw')
                if outer.delay or outer.delays.get(path):
                    time.sleep(outer.delay + outer.delays.get(path, 0))
                if outer.redirect_to:
                    self.send_response(302)
                    self.send_header('Location', outer.redirect_to)
                    self.end_headers()
                    return
                needs_auth = path != '/RouteControl.asmx'
                if needs_auth and (acct, pw) != (GOOD_ACCOUNT, GOOD_PASSWORD):
                    self.send_response(401)
                    self.end_headers()
                    return
                rows = stamp(outer.data[path], outer.age)
                payload = json.dumps(rows, ensure_ascii=False)
                if mode == 'raw':
                    out = payload + EMPTY_ENVELOPE
                elif mode == 'soap':
                    out = soap_wrap('x', payload)
                elif mode == 'fault':
                    out = ('<?xml version="1.0"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body><soap:Fault>'
                           '<faultcode>soap:Server</faultcode><faultstring>帳號或密碼錯誤</faultstring></soap:Fault></soap:Body></soap:Envelope>')
                elif mode == 'fault_other':
                    out = ('<?xml version="1.0"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body><soap:Fault>'
                           '<faultcode>soap:Server</faultcode><faultstring>' + outer.fault_text + '</faultstring></soap:Fault></soap:Body></soap:Envelope>')
                elif mode == 'fault_text':
                    out = ('<?xml version="1.0"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body><soap:Fault>'
                           '<faultstring>' + outer.fault_text + '</faultstring></soap:Fault></soap:Body></soap:Envelope>')
                elif mode == 'result_text':
                    out = soap_wrap('x', outer.result_text)
                elif mode == 'plain_text':
                    out = outer.result_text
                elif mode == 'json_string':
                    out = json.dumps(outer.result_text, ensure_ascii=False)
                elif mode == 'empty_array':
                    out = '[]' + EMPTY_ENVELOPE
                elif mode == 'garbage':
                    out = 'this is not json or xml at all'
                elif mode == 'html':
                    out = '<html><body>Server Error</body></html>'
                elif mode == 'empty':
                    out = EMPTY_ENVELOPE
                elif mode == 'notlist':
                    out = soap_wrap('x', '{"a": 1}')
                elif mode == 'doctype':
                    out = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "bbbbbbbbbb">]><r>&a;</r>'
                elif mode == 'huge':
                    out = '[' + ','.join(['{"a": "' + 'x' * 100 + '"}'] * 200) + ']'
                elif mode == '500':
                    self.send_response(500)
                    self.end_headers()
                    return
                else:
                    out = payload
                raw = out.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/xml; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def count(self, path):
        return sum(1 for r in self.requests if r[0] == path)

    def cfg(self, **extra):
        """給 helpers.config 的 metro 設定（指向這個假伺服器）。"""
        m = {'api_base': self.url, 'station_list_url': self.url + '/RouteControl.asmx', 'stations': ['台北車站']}
        m.update(extra)
        return {'metro': m}

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
