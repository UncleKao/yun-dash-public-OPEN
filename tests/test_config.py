import json
import os
import stat
import tempfile
import unittest

import helpers
import dash_config as C


class Load(unittest.TestCase):
    def test_missing_file_gives_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = C.load(os.path.join(d, 'nope.json'))
        self.assertEqual(cfg['server']['host'], '127.0.0.1')
        self.assertEqual(cfg['commute']['entities'], [])
        self.assertTrue(all(cfg['features'].values()))

    def test_override_is_deep_merged_and_comments_ignored(self):
        with helpers.config({'_說明': '註解', 'server': {'port': 12345, '_x': 1}}):
            self.assertEqual(C.CFG['server']['port'], 12345)
            self.assertEqual(C.CFG['server']['host'], '127.0.0.1')   # 沒覆寫的欄位保留預設
            self.assertNotIn('_說明', C.CFG)
            self.assertNotIn('_x', C.CFG['server'])

    def test_broken_json_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'c.json')
            with open(p, 'w') as f:
                f.write('{oops')
            with self.assertRaises(C.ConfigError):
                C.load(p)

    def test_validation(self):
        for bad in ({'server': {'port': 0}}, {'server': {'port': 'x'}}, {'utc_offset_hours': 99},
                    {'places': {'家': {'lat': 'a', 'lon': 1}}}, {'food': {'price_tiers': [500, 100]}},
                    {'commute': {'entities': 'device_tracker.x'}}):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as d:
                p = os.path.join(d, 'c.json')
                with open(p, 'w') as f:
                    json.dump(bad, f)
                with self.assertRaises(C.ConfigError):
                    C.load(p)

    def test_example_config_loads(self):
        cfg = C.load(os.path.join(helpers.ROOT, 'config.example.json'))
        self.assertIn('家', cfg['places'])


class Token(unittest.TestCase):
    def test_env_then_file_and_permission_check(self):
        with helpers.config({'ha': {'url': 'http://ha.invalid', 'token_env': 'DASH_TEST_TOKEN'}}) as d:
            os.environ['DASH_TEST_TOKEN'] = ' abc '
            try:
                self.assertEqual(C.ha_token(), 'abc')
            finally:
                del os.environ['DASH_TEST_TOKEN']
            self.assertEqual(C.ha_token(), '')
        with helpers.config() as d:
            f = os.path.join(d, 'tok')
            with open(f, 'w') as fh:
                fh.write('filetoken\n')
            os.chmod(f, 0o644)
            C.CFG['ha']['token_file'] = f
            with self.assertRaises(C.ConfigError):
                C.ha_token()
            os.chmod(f, 0o600)
            self.assertEqual(C.ha_token(), 'filetoken')


class TokenRules(unittest.TestCase):
    GOOD = 'abcDEF123._~-xyz'

    def _load(self, cfg):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'c.json')
            with open(p, 'w', encoding='utf-8') as f:
                json.dump(cfg, f)
            return C.load(p)

    def test_good_tokens_accepted(self):
        cfg = self._load({'server': {'access_token': self.GOOD}})
        self.assertEqual(cfg['server']['access_token'], self.GOOD)

    def test_bad_access_tokens_fail_at_startup(self):
        bad = ['開門芝麻開門芝麻開門芝麻', 'has space 123456', 'semi;colon-12345', 'quote"mark-12345', 'crlf\r\nX-Evil: 1-xx',
               'short', 'x' * 129, 123456789012]
        for t in bad:
            for where in ({'server': {'access_token': t}},):
                with self.subTest(token=t, where=list(where)):
                    with self.assertRaises(C.ConfigError) as cm:
                        self._load(where)
                    self.assertIn('secrets.token_urlsafe', str(cm.exception))

    def test_other_new_keys_validated(self):
        with self.assertRaises(C.ConfigError):
            self._load({'server': {'allowed_hosts': 'home.lan'}})
        with self.assertRaises(C.ConfigError):
            self._load({'server': {'tls_cert': '/only/cert.pem'}})


class TokenFileProblems(unittest.TestCase):
    def test_loose_permission_does_not_crash_the_feature_check(self):
        import dash_ha
        with helpers.config({'ha': {'url': 'http://ha.invalid', 'token_env': 'DASH_NO_SUCH_ENV'}}) as d:
            f = os.path.join(d, 'tok')
            with open(f, 'w') as fh:
                fh.write('x')
            os.chmod(f, 0o644)
            C.CFG['ha']['token_file'] = f
            self.assertFalse(dash_ha.configured())
            self.assertIn('chmod 600', dash_ha.token_problem())
            from dash_support import NotConfigured
            with self.assertRaises(NotConfigured) as cm:
                dash_ha.get('/api/')
            self.assertIn('chmod 600', cm.exception.reason)
            os.chmod(f, 0o600)
            self.assertTrue(dash_ha.configured())
            self.assertEqual(dash_ha.token_problem(), '')

    def test_unreadable_token_file_is_config_error_not_oserror(self):
        with helpers.config({'ha': {'token_env': 'DASH_NO_SUCH_ENV'}}) as d:
            os.makedirs(os.path.join(d, 'adir'))
            C.CFG['ha']['token_file'] = os.path.join(d, 'adir')       # 是資料夾，讀不了
            with self.assertRaises(C.ConfigError):
                C.ha_token()


if __name__ == '__main__':
    unittest.main()
