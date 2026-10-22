"""Python client for a Z-Road security server's internal API.

Standard library only. Typical use::

    from zroad_client import Client

    zroad = Client("http://127.0.0.1:7001", "ZW/COM/12345678/accounts-payable")
    reply = zroad.call("ZW/GOV/ZIMRA/tax-clearance/verify/v1", {"tin": "2000123456"},
                       subject="2000123456")
    cleared = reply.json()["status"] == "VALID"

Answers from the provider's own system (including its 404s) are returned as a
:class:`Reply`. Problems raised by Z-Road itself (access denied, provider
unreachable, ...) raise :class:`ZRoadError` with a stable ``code``. If your own
security server cannot be reached, :class:`Unreachable` is raised.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Union

__all__ = [
    "Client",
    "Reply",
    "Queued",
    "ZRoadError",
    "Unreachable",
    "QueueFailed",
    "QueueTimeout",
]

__version__ = "0.1.0"


class Unreachable(Exception):
    """Your own security server could not be reached."""

    retryable = True


class ZRoadError(Exception):
    """Z-Road refused or could not complete the exchange."""

    def __init__(self, status: int, code: str, message: str, retryable: bool):
        super().__init__(f"z-road error {code} (HTTP {status}): {message}")
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable


class QueueFailed(Exception):
    """A queued request ended in failure."""

    retryable = False


class QueueTimeout(Exception):
    """Gave up waiting for a queued request."""

    retryable = True


@dataclass
class Reply:
    """The provider's answer."""

    status: int
    body: bytes
    content_type: str
    message_id: str
    #: Signed evidence of the exchange, as a dict (live calls only; may be
    #: absent for very large replies). Verify it with ``zroad-cli proof-verify``
    #: or the Rust library.
    proof: Optional[dict] = None

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@dataclass
class Queued:
    """State of a request submitted with :meth:`Client.submit`."""

    state: str  # "pending" | "delivered" | "failed"
    attempts: int = 0
    last_error: Optional[str] = None
    reply: Optional[Reply] = None
    error: Optional[str] = None


Body = Union[bytes, bytearray, str, Mapping[str, Any], list, None]


class Client:
    def __init__(
        self,
        security_server: str,
        client: str,
        *,
        token: Optional[str] = None,
        shared_node: bool = False,
        timeout: float = 30.0,
    ):
        """
        :param security_server: internal URL of your organisation's security server.
        :param client: your subsystem, ``instance/class/code/subsystem``. Its
            member decides which identity the exchange is made under.
        :param token: the member's ``internal_token``, if the server requires one.
        :param shared_node: use the ``/m/<member>/...`` paths of a shared node.
        """
        parts = client.split("/")
        if len(parts) != 4 or not all(parts):
            raise ValueError("client must look like instance/class/code/subsystem")
        self._base = security_server.rstrip("/")
        self._client = client
        self._token = token
        self._prefix = "/m/" + "/".join(parts[:3]) if shared_node else ""
        self._timeout = timeout

    # -- plumbing ---------------------------------------------------------

    def _headers(self, subject, content_type, extra=None) -> dict:
        h = {"Z-Road-Client": self._client}
        if content_type:
            h["Content-Type"] = content_type
        if subject is not None:
            h["Z-Road-Subject"] = subject
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        if extra:
            h.update(extra)
        return h

    def _send(self, method: str, path: str, headers: dict, data: Optional[bytes]):
        req = urllib.request.Request(self._base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:  # non-2xx is still an answer
            return e.code, e.headers, e.read()
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise Unreachable(f"cannot reach the security server: {e}") from e

    @staticmethod
    def _encode(body: Body):
        if body is None:
            return b"", None
        if isinstance(body, (bytes, bytearray)):
            return bytes(body), None
        if isinstance(body, str):
            return body.encode("utf-8"), None
        return json.dumps(body).encode("utf-8"), "application/json"

    @staticmethod
    def _raise_if_zroad_error(status: int, headers, body: bytes) -> None:
        # Errors raised by Z-Road carry this header; that is how they are told
        # apart from an error status returned by the provider's own system.
        if headers.get("Z-Road-Error") is None:
            return
        try:
            payload = json.loads(body.decode("utf-8"))
            code, message = payload.get("error", "unknown"), payload.get("message", "")
        except (ValueError, UnicodeDecodeError, AttributeError):
            code, message = "unknown", body[:200].decode("utf-8", errors="replace")
        raise ZRoadError(status, code, message, retryable=headers.get("Retry-After") is not None)

    # -- API --------------------------------------------------------------

    def call(
        self,
        service: str,
        body: Body = None,
        *,
        subject: Optional[str] = None,
        content_type: Optional[str] = None,
    ) -> Reply:
        """Call ``service`` (``instance/class/code/subsystem/service/version``) and wait."""
        data, default_ct = self._encode(body)
        headers = self._headers(subject, content_type or default_ct or "application/json")
        status, h, raw = self._send("POST", f"{self._prefix}/r1/{service}", headers, data)
        self._raise_if_zroad_error(status, h, raw)
        proof = None
        if h.get("Z-Road-Proof"):
            try:
                proof = json.loads(base64.b64decode(h["Z-Road-Proof"]))
            except (ValueError, TypeError):
                proof = None
        return Reply(
            status=status,
            body=raw,
            content_type=h.get("Content-Type", "application/json"),
            message_id=h.get("Z-Road-Message-Id", ""),
            proof=proof,
        )

    def submit(
        self,
        service: str,
        body: Body = None,
        *,
        subject: Optional[str] = None,
        content_type: Optional[str] = None,
    ) -> str:
        """Queue a request on the security server's durable outbox; returns the
        message id immediately. The server retries with backoff, safely."""
        data, default_ct = self._encode(body)
        headers = self._headers(
            subject, content_type or default_ct or "application/json", {"Z-Road-Mode": "async"}
        )
        status, h, raw = self._send("POST", f"{self._prefix}/r1/{service}", headers, data)
        self._raise_if_zroad_error(status, h, raw)
        if status != 202:
            raise ZRoadError(status, "unexpected_status", f"expected 202, got {status}", False)
        return json.loads(raw.decode("utf-8"))["message_id"]

    def poll(self, message_id: str) -> Queued:
        status, h, raw = self._send(
            "GET", f"{self._prefix}/internal/v1/outbox/{message_id}", self._headers(None, None), None
        )
        self._raise_if_zroad_error(status, h, raw)
        if status != 200:
            raise ZRoadError(status, "unexpected_status", f"HTTP {status}", False)
        v = json.loads(raw.decode("utf-8"))
        if v.get("state") == "pending":
            item = v.get("item", {})
            return Queued("pending", attempts=item.get("attempts", 0), last_error=item.get("last_error"))
        result = v.get("result", {})
        if result.get("state") == "delivered":
            body = base64.b64decode(result.get("body_b64") or "")
            return Queued(
                "delivered",
                reply=Reply(
                    status=result.get("status") or 0,
                    body=body,
                    content_type=result.get("content_type") or "application/json",
                    message_id=message_id,
                ),
            )
        return Queued("failed", error=result.get("error") or "failed")

    def wait(self, message_id: str, max_wait: float = 300.0, interval: float = 0.5) -> Reply:
        """Poll until a queued request is delivered, fails, or ``max_wait`` passes."""
        deadline = time.monotonic() + max_wait
        while True:
            q = self.poll(message_id)
            if q.state == "delivered":
                assert q.reply is not None
                return q.reply
            if q.state == "failed":
                raise QueueFailed(q.error)
            if time.monotonic() >= deadline:
                raise QueueTimeout(f"{message_id} still pending after {max_wait}s")
            time.sleep(interval)
