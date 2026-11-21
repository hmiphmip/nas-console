#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""访问控制回归测试 — 仅用标准库，不依赖 docker / n8n / ffmpeg。

覆盖：
1. 回环地址 + 未设置令牌 → 放行（保持本地单机可用）
2. 设置了令牌 → 无令牌 / 错误令牌一律 401
3. 三种令牌携带方式（Bearer / X-Console-Token / ?token=）均可通过
4. 非回环地址 + 未设置令牌 → 进程拒绝启动（退出码 2）

运行：python3 -m unittest discover -s tests -v
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import http.cookiejar
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / 'console_server.py'
TOKEN = 'test-token-9f2c'


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class ServerFixture:
    """在临时工作目录里拉起一个真实的服务进程。"""

    def __init__(self, extra_env=None):
        self.port = free_port()
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.base = base
        env = dict(os.environ)
        env.update({
            'CONSOLE_BASE': str(base),
            'WF_ROOT': str(base / 'workflows'),
            'CONSOLE_PORT': str(self.port),
            'CONSOLE_BIND': '127.0.0.1',
        })
        env.pop('CONSOLE_TOKEN', None)
        env.update(extra_env or {})
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def wait_ready(self, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError('服务提前退出: %s' % self.proc.stderr.read().decode('utf-8', 'replace'))
            try:
                with socket.create_connection(('127.0.0.1', self.port), 0.5):
                    return
            except OSError:
                time.sleep(0.15)
        raise RuntimeError('服务在 %ss 内未就绪' % timeout)

    def get(self, path, headers=None):
        req = urllib.request.Request('http://127.0.0.1:%d%s' % (self.port, path),
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        for f in (self.proc.stdout, self.proc.stderr):
            if f:
                f.close()
        self.tmp.cleanup()


class NoTokenLoopbackTest(unittest.TestCase):
    """默认部署：仅回环、无令牌，本机可用。"""

    @classmethod
    def setUpClass(cls):
        cls.s = ServerFixture()
        cls.s.wait_ready()

    @classmethod
    def tearDownClass(cls):
        cls.s.stop()

    def test_api_reachable_without_token(self):
        code, body = self.s.get('/api/config')
        self.assertEqual(code, 200)
        self.assertIn('topic', json.loads(body))


class TokenRequiredTest(unittest.TestCase):
    """启用令牌后，任何未携带或携带错误令牌的请求都必须 401。"""

    @classmethod
    def setUpClass(cls):
        cls.s = ServerFixture({'CONSOLE_TOKEN': TOKEN})
        cls.s.wait_ready()

    @classmethod
    def tearDownClass(cls):
        cls.s.stop()

    def test_missing_token_401(self):
        code, _ = self.s.get('/api/config')
        self.assertEqual(code, 401)

    def test_wrong_token_401(self):
        code, _ = self.s.get('/api/config', {'X-Console-Token': 'wrong'})
        self.assertEqual(code, 401)

    def test_bearer_token_ok(self):
        code, _ = self.s.get('/api/config', {'Authorization': 'Bearer ' + TOKEN})
        self.assertEqual(code, 200)

    def test_header_token_ok(self):
        code, _ = self.s.get('/api/config', {'X-Console-Token': TOKEN})
        self.assertEqual(code, 200)

    def test_query_token_ok(self):
        code, _ = self.s.get('/api/config?token=' + TOKEN)
        self.assertEqual(code, 200)

    def test_query_token_sets_cookie_for_subresources(self):
        """首次带 ?token= 访问后，<script>/<video> 这类子资源靠 Cookie 通过鉴权。"""
        self.s.base.joinpath('style.css').write_text('body{}', encoding='utf-8')
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        base_url = 'http://127.0.0.1:%d' % self.s.port

        r = opener.open(base_url + '/api/config?token=' + TOKEN, timeout=10)
        self.assertEqual(r.status, 200)
        r.read()
        self.assertIn('console_token', [c.name for c in jar])

        r2 = opener.open(base_url + '/static/style.css', timeout=10)
        self.assertEqual(r2.status, 200)
        r2.read()

    def test_post_requires_token(self):
        req = urllib.request.Request(
            'http://127.0.0.1:%d/api/config' % self.s.port,
            data=json.dumps({'topic': {'query': 'x'}}).encode(),
            headers={'Content-Type': 'application/json'}, method='POST')
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail('未鉴权的 POST 不应成功')
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)


class InsecureBindRefusedTest(unittest.TestCase):
    """非回环地址且无令牌 → 必须拒绝启动。"""

    def test_refuses_non_loopback_without_token(self):
        tmp = tempfile.TemporaryDirectory()
        env = dict(os.environ)
        env.update({
            'CONSOLE_BASE': tmp.name,
            'WF_ROOT': str(Path(tmp.name) / 'workflows'),
            'CONSOLE_PORT': str(free_port()),
            'CONSOLE_BIND': '0.0.0.0',
        })
        env.pop('CONSOLE_TOKEN', None)
        try:
            p = subprocess.run([sys.executable, str(SERVER)], env=env,
                               capture_output=True, timeout=20)
            self.assertEqual(p.returncode, 2, p.stderr.decode('utf-8', 'replace'))
            self.assertIn('CONSOLE_TOKEN', p.stderr.decode('utf-8', 'replace'))
        finally:
            tmp.cleanup()


if __name__ == '__main__':
    unittest.main(verbosity=2)
