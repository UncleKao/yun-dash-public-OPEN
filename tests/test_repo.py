"""版本庫衛生：個人設定與資料不能被 git 追蹤。"""
import os
import shutil
import subprocess
import unittest

import helpers


@unittest.skipUnless(shutil.which('git') and os.path.isdir(os.path.join(helpers.ROOT, '.git')), '需要 git 與版本庫')
class GitIgnore(unittest.TestCase):
    def ignored(self, path):
        r = subprocess.run(['git', 'check-ignore', '-q', path], cwd=helpers.ROOT)
        return r.returncode == 0

    def test_personal_files_are_ignored(self):
        for p in ('config.json', 'myconfig.json', 'config.home.json', 'config.local.json', 'data/locations.sqlite',
                  'data/notes.json', 'x.sqlite', '.env', 'a.bak', 'tools/__pycache__/x.pyc'):
            with self.subTest(path=p):
                self.assertTrue(self.ignored(p), p)

    def test_example_config_is_not_ignored(self):
        self.assertFalse(self.ignored('config.example.json'))

    def test_nothing_personal_is_tracked(self):
        out = subprocess.run(['git', 'ls-files'], cwd=helpers.ROOT, capture_output=True, text=True).stdout.split('\n')
        for f in out:
            self.assertFalse(f.startswith('data/') or f == 'config.json' or f.endswith('.sqlite'), f)


if __name__ == '__main__':
    unittest.main()
