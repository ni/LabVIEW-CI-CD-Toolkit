import ast
import http.client
import io
import json
import pathlib
import sys
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'actions' / 'dashboard' / 'dashboard.py'
TREE = ast.parse(SOURCE.read_text(encoding='utf-8'))
FUNCTIONS = [
    node for node in TREE.body
    if isinstance(node, ast.FunctionDef) and node.name in {'_retry_delay', 'gh_get'}
]
DASHBOARD = {
    'json': json,
    'repo': 'owner/repo',
    'sys': sys,
    'time': time,
    'token': 'test-token',
    'urllib': urllib,
    '_API': {'degraded': False},
}
exec(compile(ast.Module(body=FUNCTIONS, type_ignores=[]), str(SOURCE), 'exec'), DASHBOARD)


class DashboardApiTests(unittest.TestCase):
    def test_remote_disconnect_retries_and_returns_response(self):
        response = io.StringIO('{"ok": true}')
        with patch('urllib.request.urlopen',
                   side_effect=[http.client.RemoteDisconnected('connection dropped'), response]) as request:
            with patch('time.sleep') as sleep:
                result = DASHBOARD['gh_get']('commits/sha', _tries=2)

        self.assertEqual(result, {'ok': True})
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(1)
        self.assertFalse(DASHBOARD['_API']['degraded'])


if __name__ == '__main__':
    unittest.main()
