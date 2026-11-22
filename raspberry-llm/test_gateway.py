import contextlib
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import socket
import select
import tempfile
import threading
import time
import unittest
from urllib.parse import urlencode

import access
import gateway


class Backend(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        body = b'{"object":"list","data":[]}' if self.path == "/v1/models" else b"<html>chat</html>"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if data.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if data["messages"][0]["content"] == "idle":
                self.wfile.flush()
                for _ in range(200):
                    if select.select([self.connection], [], [], .05)[0]:
                        if not self.connection.recv(1, socket.MSG_PEEK):
                            self.server.cancelled.set()
                            return
                return
            text = 'data: {"choices":[{"delta":{"content":"Привет"}}]}\n\n'.encode()
            for part in (text[:17], text[17:]):
                self.wfile.write(part)
                self.wfile.flush()
                time.sleep(0.03)
            if data["messages"][0]["content"] == "hold":
                for _ in range(100):
                    try:
                        self.wfile.write(b": hold\n\n")
                        self.wfile.flush()
                    except OSError:
                        return
                    time.sleep(0.05)
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="llm-gateway-test-")
        directory = Path(cls.temporary.name)
        cls.config_path = directory / "access.json"
        cls.config = {"origin": "https://chat.example.test", "prefix": "/llm", "username": "owner",
                      "password": access.password_record("correct test password"), "session_secret": "ab" * 32,
                      "api_keys": {"test": hashlib.sha256(b"test-key").hexdigest()}}
        access.write_private(cls.config_path, cls.config)
        cls.backend = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        cls.front = gateway.Gateway(("127.0.0.1", 0), cls.config_path, cls.backend.server_port)
        for server in (cls.backend, cls.front):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.front, cls.backend):
            server.shutdown()
            server.server_close()
        cls.temporary.cleanup()

    def request(self, method, path, data=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.front.server_port, timeout=10)
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            headers = {"Content-Type": "application/json", **(headers or {})}
        connection.request(method, path, body=data, headers=headers or {})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def payload(self, streaming=False, content="hello"):
        return {"model": "raspberry-llm", "messages": [{"role": "user", "content": content}], "max_tokens": 32, "stream": streaming}

    def test_unauthenticated_routes(self):
        for path in ("/v1/models", "/llm/props", "/llm/health"):
            self.assertEqual(self.request("GET", path)[0], 401)
        status, headers, _ = self.request("GET", "/llm/")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/llm/login")
        self.assertEqual(self.request("POST", "/v1/chat/completions", self.payload())[0], 401)

    def test_api_key_and_revocation(self):
        self.assertEqual(self.request("GET", "/v1/models", headers={"Authorization": "Bearer bad"})[0], 401)
        self.assertEqual(self.request("GET", "/v1/models", headers={"Authorization": "Bearer test-key"})[0], 200)
        changed = {**self.config, "api_keys": {}}
        access.write_private(self.config_path, changed)
        try:
            self.assertEqual(self.request("GET", "/v1/models", headers={"Authorization": "Bearer test-key"})[0], 401)
        finally:
            access.write_private(self.config_path, self.config)

    def test_login_cookie_csrf_and_logout(self):
        body = urlencode({"username": "owner", "password": "correct test password"})
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Origin": self.config["origin"]}
        self.assertEqual(self.request("POST", "/llm/login", body, {**headers, "Origin": "https://evil.example"})[0], 403)
        status, result_headers, _ = self.request("POST", "/llm/login", body, headers)
        self.assertEqual(status, 200)
        cookie = result_headers["Set-Cookie"]
        for attribute in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(attribute, cookie)
        cookie = cookie.split(";")[0]
        self.assertEqual(self.request("GET", "/llm/", headers={"Cookie": cookie})[0], 200)
        self.assertEqual(self.request("POST", "/v1/chat/completions", self.payload(), {"Cookie": cookie})[0], 403)
        self.assertEqual(self.request("POST", "/v1/chat/completions", self.payload(), {"Cookie": cookie, "Origin": self.config["origin"]})[0], 200)
        self.assertEqual(self.request("POST", "/llm/logout", b"", {"Cookie": cookie, "Origin": self.config["origin"]})[0], 200)
        self.assertEqual(self.request("GET", "/v1/models", headers={"Cookie": cookie})[0], 401)
        changed = {**self.config, "session_secret": "cd" * 32}
        access.write_private(self.config_path, changed)
        try:
            self.assertEqual(self.request("GET", "/v1/models", headers={"Cookie": cookie})[0], 401)
        finally:
            access.write_private(self.config_path, self.config)

    def test_resource_and_route_limits(self):
        headers = {"Authorization": "Bearer test-key"}
        payload = self.payload()
        payload["max_tokens"] = 99999
        self.assertEqual(self.request("POST", "/v1/chat/completions", payload, headers)[0], 400)
        self.assertEqual(self.request("POST", "/llm/tools", {"command": "anything"}, headers)[0], 404)
        self.assertEqual(self.request("GET", "/llm/metrics", headers=headers)[0], 404)

    def test_streaming_and_busy(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.front.server_port, timeout=10)
        connection.request("POST", "/v1/chat/completions", json.dumps(self.payload(True, "hold")),
                           {"Authorization": "Bearer test-key", "Content-Type": "application/json"})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        status, headers, _ = self.request("POST", "/v1/chat/completions", self.payload(), {"Authorization": "Bearer test-key"})
        self.assertEqual(status, 429)
        self.assertEqual(headers["Retry-After"], "10")
        body = response.read()
        self.assertIn("Привет".encode(), body)
        self.assertIn(b"data: [DONE]", body)
        connection.close()
        self.assertEqual(self.request("POST", "/v1/chat/completions", self.payload(), {"Authorization": "Bearer test-key"})[0], 200)

    def test_cookie_tampering_and_expiration(self):
        token = gateway.issue_session(self.config)
        self.assertTrue(gateway.valid_session(token, self.config))
        self.assertFalse(gateway.valid_session(token + "wrong", self.config))
        self.assertFalse(gateway.valid_session("0.expired.signature", self.config))
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o600)

    def test_native_ui_and_openai_text_parts(self):
        payload = {"messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
                   "backend_sampling": True, "reasoning_control": True, "reasoning_format": "auto",
                   "return_progress": True, "sse_ping_interval": 5, "stream": False, "timings_per_token": True, "tools": []}
        status, _, _ = self.request("POST", "/llm/v1/chat/completions", payload, {"Authorization": "Bearer test-key"})
        self.assertEqual(status, 200)

    def test_cancel_interrupts_silent_upstream(self):
        self.backend.cancelled = threading.Event()
        connection = http.client.HTTPConnection("127.0.0.1", self.front.server_port, timeout=10)
        connection.request("POST", "/v1/chat/completions", json.dumps(self.payload(True, "idle")),
                           {"Authorization":"Bearer test-key","Content-Type":"application/json"})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        response.read(1)
        response.close()
        connection.close()
        self.assertTrue(self.backend.cancelled.wait(8), "Disconnect did not interrupt the silent upstream")


if __name__ == "__main__":
    unittest.main()
