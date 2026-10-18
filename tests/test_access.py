import email
import unittest

import helpers   # noqa: F401
from dash_access import Access


def hdr(**kw):
    return email.message_from_string(''.join(f'{k.replace("_", "-")}: {v}\n' for k, v in kw.items()) + '\n')


def cfg(**kw):
    base = {'allowed_networks': ['127.0.0.1/32', '::1/128'], 'access_token': '', 'allowed_hosts': []}
    base.update(kw)
    return base


TOK = 'tok-abcdefghij123'


class Network(unittest.TestCase):
    def test_default_allows_only_loopback(self):
        a = Access(cfg())
        self.assertTrue(a.ip_ok('127.0.0.1'))
        self.assertTrue(a.ip_ok('::1'))
        self.assertTrue(a.ip_ok('::ffff:127.0.0.1'))
        self.assertFalse(a.ip_ok('192.168.1.5'))
        self.assertFalse(a.ip_ok('not-an-ip'))

    def test_cidr(self):
        a = Access(cfg(allowed_networks=['192.168.1.0/24']))
        self.assertTrue(a.ip_ok('192.168.1.200'))
        self.assertFalse(a.ip_ok('192.168.2.1'))

    def test_bad_network_rejected(self):
        with self.assertRaises(ValueError):
            Access(cfg(allowed_networks=['nope']))


class HostCheck(unittest.TestCase):
    """擋 DNS 重綁：Host 只接受 IP 字面值、localhost、與 allowed_hosts。"""

    def test_accepts_ip_and_localhost(self):
        a = Access(cfg())
        for h in ('127.0.0.1:9877', '127.0.0.1', 'localhost:9877', 'LOCALHOST', '[::1]:9877', '192.168.1.20:80'):
            with self.subTest(host=h):
                self.assertTrue(a.host_ok(hdr(Host=h)))

    def test_rejects_unknown_names_and_missing(self):
        a = Access(cfg())
        for h in ('attacker.example:9877', 'evil.com', 'localhost.evil.com', '127.0.0.1.evil.com', ''):
            with self.subTest(host=h):
                self.assertFalse(a.host_ok(hdr(Host=h) if h else hdr()))

    def test_allowed_hosts_case_insensitive(self):
        a = Access(cfg(allowed_hosts=['Home-Dash.lan']))
        self.assertTrue(a.host_ok(hdr(Host='home-dash.LAN:9877')))
        self.assertFalse(a.host_ok(hdr(Host='other.lan')))


class Token(unittest.TestCase):
    def test_header_and_session_cookie(self):
        a = Access(cfg(access_token=TOK))
        self.assertFalse(a.token_ok(hdr()))
        self.assertFalse(a.token_ok(hdr(X_Dash_Token='wrong')))
        self.assertTrue(a.token_ok(hdr(X_Dash_Token=TOK)))
        cookie = a.cookie_header().split(';')[0]          # dash_token=<hmac>
        self.assertNotIn(TOK, cookie)                      # cookie 不是密語本身
        self.assertTrue(a.token_ok(hdr(Cookie='a=b; ' + cookie)))
        self.assertFalse(a.token_ok(hdr(Cookie='dash_token=' + TOK)))   # 直接拿密語當 cookie 不行
        self.assertFalse(a.token_ok(hdr(Cookie='dash_token=nope')))

    def test_cookie_for_other_token_rejected(self):
        a, b = Access(cfg(access_token=TOK)), Access(cfg(access_token=TOK + 'x'))
        self.assertFalse(b.token_ok(hdr(Cookie=a.cookie_header().split(';')[0])))

    def test_non_ascii_input_does_not_crash(self):
        a = Access(cfg(access_token=TOK))
        self.assertFalse(a.token_ok(hdr(X_Dash_Token='開門芝麻')))
        self.assertFalse(a.check_login('開門芝麻'))
        self.assertTrue(a.check_login(TOK))
        self.assertFalse(a.check_login(''))

    def test_cookie_flags(self):
        plain = Access(cfg(access_token=TOK)).cookie_header()
        self.assertIn('HttpOnly', plain)
        self.assertIn('SameSite=Strict', plain)
        self.assertNotIn('Secure', plain)
        for extra in ({'public_https': True}, {'tls_cert': '/x.pem', 'tls_key': '/x.key'}):
            with self.subTest(extra=extra):
                self.assertIn('Secure', Access(cfg(access_token=TOK, **extra)).cookie_header())

    def test_no_token_configured_means_open_within_network(self):
        a = Access(cfg())
        self.assertTrue(a.token_ok(hdr()))
        self.assertFalse(a.check_login('anything'))


class OriginCheck(unittest.TestCase):
    def test_no_origin_passes(self):
        self.assertTrue(Access(cfg()).same_origin(hdr(Host='h:1')))     # curl／HA

    def test_scheme_host_and_port_must_match(self):
        a = Access(cfg())
        self.assertTrue(a.same_origin(hdr(Host='h:1', Origin='http://h:1')))
        self.assertTrue(a.same_origin(hdr(Host='H:1', Origin='http://h:1')))
        self.assertFalse(a.same_origin(hdr(Host='h:1', Origin='https://h:1')))      # 協定不同
        self.assertFalse(a.same_origin(hdr(Host='h:1', Origin='http://h:2')))       # 埠不同
        self.assertFalse(a.same_origin(hdr(Host='h:1', Origin='http://evil.example')))
        self.assertFalse(a.same_origin(hdr(Host='h:1', Origin='null')))
        self.assertFalse(a.same_origin(hdr(Host='h:1', Origin='http://h:1/path')))

    def test_https_deployment_expects_https_origin(self):
        a = Access(cfg(public_https=True))
        self.assertTrue(a.same_origin(hdr(Host='h', Origin='https://h')))
        self.assertFalse(a.same_origin(hdr(Host='h', Origin='http://h')))

    def test_sec_fetch_site(self):
        a = Access(cfg())
        self.assertTrue(a.same_origin(hdr(Host='h:1', Sec_Fetch_Site='same-origin')))
        self.assertTrue(a.same_origin(hdr(Host='h:1', Sec_Fetch_Site='none')))
        self.assertFalse(a.same_origin(hdr(Host='h:1', Sec_Fetch_Site='cross-site')))
        self.assertFalse(a.same_origin(hdr(Host='h:1', Sec_Fetch_Site='same-site')))


if __name__ == '__main__':
    unittest.main()
