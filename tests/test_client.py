"""Hermetic tests: a scripted fake security server, no Z-Road needed."""

import base64
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from zroad_client import Client, QueueFailed, QueueTimeout, Unreachable, ZRoadError

SVC = "ZW/GOV/ZIMRA/tax-clearance/verify/v1"
ME = "ZW/COM/12345678/accounts-payable"


class Fake:
    """Serve scripted responses and record requests."""

    def __init__(self):
        self.requests = []
        self.script = []  # list of (status, headers, body) consumed in order
        fake = self

        class H(BaseHTTPRequestHandler):
            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                fake.requests.append(
                    {"method": self.command, "path": self.path, "headers": dict(self.headers), "body": self.rfile.read(n)}
                )
                status, headers, body = fake.script.pop(0)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _do

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.fake = Fake()
        self.addCleanup(self.fake.close)

    def client(self, **kw):
        return Client(self.fake.url, ME, **kw)

    def test_call_returns_provider_answer_and_proof(self):
        proof = {"request": {"payload_b64": "x"}}
        self.fake.script.append(
            (
                200,
                {
                    "Content-Type": "application/json",
                    "Z-Road-Message-Id": "abc123",
                    "Z-Road-Proof": base64.b64encode(json.dumps(proof).encode()).decode(),
                },
                b'{"status":"VALID"}',
            )
        )
        reply = self.client().call(SVC, {"tin": "2000123456"}, subject="2000123456")
        self.assertTrue(reply.ok)
        self.assertEqual(reply.json()["status"], "VALID")
        self.assertEqual(reply.message_id, "abc123")
        self.assertEqual(reply.proof, proof)
        req = self.fake.requests[0]
        self.assertEqual((req["method"], req["path"]), ("POST", f"/r1/{SVC}"))
        self.assertEqual(req["headers"]["Z-Road-Client"], ME)
        self.assertEqual(req["headers"]["Z-Road-Subject"], "2000123456")
        self.assertEqual(json.loads(req["body"]), {"tin": "2000123456"})
        self.assertEqual(req["headers"]["Content-Type"], "application/json")

    def test_provider_404_is_a_reply_not_an_error(self):
        self.fake.script.append((404, {}, b'{"status":"NOT_FOUND"}'))
        reply = self.client().call(SVC, {"tin": "0"})
        self.assertEqual(reply.status, 404)
        self.assertFalse(reply.ok)
        self.assertEqual(reply.json()["status"], "NOT_FOUND")

    def test_zroad_errors_raise_with_code_and_retryability(self):
        body = json.dumps({"error": "access_denied", "message": "no"}).encode()
        self.fake.script.append((403, {"Z-Road-Error": "access_denied"}, body))
        with self.assertRaises(ZRoadError) as cm:
            self.client().call(SVC, {})
        self.assertEqual((cm.exception.status, cm.exception.code, cm.exception.retryable), (403, "access_denied", False))

        body = json.dumps({"error": "provider_unreachable", "message": "down"}).encode()
        self.fake.script.append((503, {"Z-Road-Error": "provider_unreachable", "Retry-After": "5"}, body))
        with self.assertRaises(ZRoadError) as cm:
            self.client().call(SVC, {})
        self.assertEqual(cm.exception.code, "provider_unreachable")
        self.assertTrue(cm.exception.retryable)

    def test_unreadable_zroad_error_body(self):
        self.fake.script.append((502, {"Z-Road-Error": "x"}, b"<html>oops</html>"))
        with self.assertRaises(ZRoadError) as cm:
            self.client().call(SVC, {})
        self.assertEqual(cm.exception.code, "unknown")

    def test_unreachable_security_server(self):
        c = Client("http://127.0.0.1:1", ME, timeout=2)
        with self.assertRaises(Unreachable):
            c.call(SVC, {})

    def test_token_and_body_types(self):
        self.fake.script += [(200, {}, b"{}"), (200, {}, b"{}"), (200, {}, b"{}")]
        c = self.client(token="secret")
        c.call(SVC, b"raw bytes", content_type="application/octet-stream")
        c.call(SVC, "text body")
        c.call(SVC, None)
        r = self.fake.requests
        self.assertEqual(r[0]["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(r[0]["body"], b"raw bytes")
        self.assertEqual(r[0]["headers"]["Content-Type"], "application/octet-stream")
        self.assertEqual(r[1]["body"], b"text body")
        self.assertEqual(r[2]["body"], b"")

    def test_shared_node_prefix(self):
        self.fake.script.append((200, {}, b"{}"))
        self.client(shared_node=True).call(SVC, {})
        self.assertEqual(self.fake.requests[0]["path"], f"/m/ZW/COM/12345678/r1/{SVC}")

    def test_submit_poll_wait(self):
        self.fake.script.append((202, {}, b'{"message_id":"m1","status_url":"/internal/v1/outbox/m1"}'))
        c = self.client()
        self.assertEqual(c.submit(SVC, {"tin": "1"}), "m1")
        self.assertEqual(self.fake.requests[0]["headers"]["Z-Road-Mode"], "async")

        pending = {"state": "pending", "item": {"attempts": 2, "last_error": "provider_unreachable: down"}}
        self.fake.script.append((200, {}, json.dumps(pending).encode()))
        q = c.poll("m1")
        self.assertEqual((q.state, q.attempts, q.last_error), ("pending", 2, "provider_unreachable: down"))

        delivered = {
            "state": "finished",
            "result": {
                "state": "delivered",
                "status": 200,
                "body_b64": base64.b64encode(b'{"status":"VALID"}').decode(),
                "content_type": "application/json",
            },
        }
        self.fake.script += [(200, {}, json.dumps(pending).encode()), (200, {}, json.dumps(delivered).encode())]
        reply = c.wait("m1", max_wait=10, interval=0.01)
        self.assertEqual(reply.json()["status"], "VALID")

    def test_wait_failure_and_timeout(self):
        failed = {"state": "finished", "result": {"state": "failed", "error": "access_denied: no"}}
        self.fake.script.append((200, {}, json.dumps(failed).encode()))
        with self.assertRaises(QueueFailed):
            self.client().wait("m2", max_wait=5, interval=0.01)

        pending = json.dumps({"state": "pending", "item": {}}).encode()
        self.fake.script += [(200, {}, pending)] * 50
        with self.assertRaises(QueueTimeout):
            self.client().wait("m3", max_wait=0.05, interval=0.01)

    def test_client_id_is_validated(self):
        for bad in ["ZW/COM/1", "ZW/COM/1/", "a/b/c/d/e"]:
            with self.assertRaises(ValueError):
                Client("http://x", bad)


if __name__ == "__main__":
    unittest.main()
