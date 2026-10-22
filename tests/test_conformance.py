"""Runs the language-neutral SDK conformance scenarios from the Z-Road repository
(guides/sdk-conformance/scenarios.json, copied to tests/conformance/). A worked
example of a runner for anyone writing an SDK in another language."""

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from zroad_client import Client, QueueFailed, QueueTimeout, Unreachable, ZRoadError

HERE = os.path.dirname(__file__)
SPEC = json.load(open(os.path.join(HERE, "conformance", "scenarios.json")))
CONST = SPEC["constants"]


def subst(value):
    if isinstance(value, str):
        return value.replace("{client}", CONST["client"]).replace("{service}", CONST["service"])
    if isinstance(value, dict):
        return {k: subst(v) for k, v in value.items()}
    if isinstance(value, list):
        return [subst(v) for v in value]
    return value


class FakeServer:
    def __init__(self, responses, repeat_last):
        self.requests, self.responses, self.repeat_last = [], list(responses), repeat_last
        fake = self

        class H(BaseHTTPRequestHandler):
            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                fake.requests.append(
                    {"method": self.command, "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": self.rfile.read(n)}
                )
                r = fake.responses[0] if (fake.repeat_last and len(fake.responses) == 1) else fake.responses.pop(0)
                body = r["body"]
                data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                self.send_response(r["status"])
                for k, v in r.get("headers", {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _do

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def outcome(sc, client):
    op = subst(sc["operation"])
    t = op["type"]
    if t == "call":
        return client.call(op["service"], op.get("body"), subject=op.get("subject"))
    if t == "submit":
        return client.submit(op["service"], op.get("body"), subject=op.get("subject"))
    if t == "poll":
        return client.poll(op["message_id"])
    if t == "wait":
        return client.wait(op["message_id"], max_wait=op["max_wait_secs"], interval=op["interval_secs"])
    raise AssertionError(t)


class Conformance(unittest.TestCase):
    pass


def make_test(sc):
    def test(self):
        fake = None
        if sc.get("server_down"):
            url = "http://127.0.0.1:1"
        else:
            fake = FakeServer([subst(r) for r in sc["responses"]], sc.get("repeat_last_response", False))
            self.addCleanup(fake.close)
            url = fake.url
        opts = sc.get("client_options", {})
        client = Client(url, CONST["client"], token=opts.get("token"), shared_node=opts.get("shared_node", False), timeout=2)
        exp = sc["expect"]
        try:
            got = outcome(sc, client)
        except ZRoadError as e:
            want = exp["error"]
            self.assertEqual(want["kind"], "zroad")
            self.assertEqual((e.status, e.code, e.retryable), (want["status"], want["code"], want["retryable"]))
        except Unreachable as e:
            self.assertEqual(exp["error"]["kind"], "unreachable")
            self.assertTrue(e.retryable)
        except QueueFailed:
            self.assertEqual(exp["error"]["kind"], "queue_failed")
        except QueueTimeout as e:
            self.assertEqual(exp["error"]["kind"], "timeout")
            self.assertTrue(e.retryable)
        else:
            self.assertNotIn("error", exp, "expected an error but the call succeeded")
            if "message_id" in exp:
                self.assertEqual(got, exp["message_id"])
            elif "queued" in exp:
                w = exp["queued"]
                self.assertEqual(got.state, w["state"])
                if "attempts" in w:
                    self.assertEqual(got.attempts, w["attempts"])
                if "last_error" in w:
                    self.assertEqual(got.last_error, w["last_error"])
                if "error" in w:
                    self.assertEqual(got.error, w["error"])
                if "reply" in w:
                    self.assertEqual(got.reply.status, w["reply"]["status"])
                    self.assertEqual(got.reply.json(), w["reply"]["json"])
            else:
                w = exp["reply"]
                if "status" in w:
                    self.assertEqual(got.status, w["status"])
                if "ok" in w:
                    self.assertEqual(got.ok, w["ok"])
                if "json" in w:
                    self.assertEqual(got.json(), w["json"])
                if "message_id" in w:
                    self.assertEqual(got.message_id, w["message_id"])
                if "proof_present" in w:
                    self.assertEqual(got.proof is not None, w["proof_present"])
        for i, want in enumerate(sc.get("expect_requests", [])):
            want = subst(want)
            seen = fake.requests[i]
            self.assertEqual(seen["method"], want["method"])
            self.assertEqual(seen["path"], want["path"])
            for k, v in want.get("headers", {}).items():
                self.assertEqual(seen["headers"].get(k.lower()), v, f"header {k}")
            if "json_body" in want:
                self.assertEqual(json.loads(seen["body"]), want["json_body"])

    return test


for _sc in SPEC["scenarios"]:
    setattr(Conformance, "test_" + _sc["name"], make_test(_sc))


class InvalidClients(unittest.TestCase):
    def test_constructor_rejects_bad_client_ids(self):
        for bad in SPEC["invalid_clients"]:
            with self.assertRaises(ValueError):
                Client("http://x", bad)


if __name__ == "__main__":
    unittest.main()
