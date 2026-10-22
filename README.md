# zroad-client (Python)

A small Python client for the internal API of a [Z-Road](https://github.com/phoscoder/z-road)
security server. It is for application developers (ERP, accounting, banking,
portals) who want to call another organisation's service through their own
security server. Standard library only; Python 3.9 or newer.

Your application never speaks the Z-Road protocol. It makes plain HTTP calls to
**its own** security server, which signs, encrypts, logs and delivers. This
library wraps those calls and turns Z-Road's error conventions into exceptions.

## Install

Not published to PyPI yet. From a checkout:

```sh
pip install -e .
```

## Use

```python
from zroad_client import Client, ZRoadError, Unreachable

zroad = Client("http://127.0.0.1:7001", "ZW/COM/12345678/accounts-payable")

try:
    reply = zroad.call(
        "ZW/GOV/ZIMRA/tax-clearance/verify/v1",
        {"tin": "2000123456"},
        subject="2000123456",       # who the query is about (optional)
    )
except ZRoadError as e:
    print(e.code, e.retryable)      # e.g. "access_denied", False
except Unreachable:
    print("our own security server is down")
else:
    cleared = reply.ok and reply.json()["status"] == "VALID"
```

* `Client(security_server, client, token=None, shared_node=False, timeout=30)`:
  `client` is your subsystem, `instance/class/code/subsystem`. Pass `token` if your
  security server has an `internal_token`. Use `shared_node=True` when the
  server is a shared node hosting several members.
* `reply.status` / `reply.json()` / `reply.body` is **the provider's own answer**,
  including its error statuses. A 404 "TIN not found" from the provider is a
  normal `Reply`, not an exception.
* **Z-Road's own errors** raise `ZRoadError` with a stable `code`
  (`access_denied`, `unknown_provider`, `provider_unreachable`, `rate_limited`,
  `invalid_request`, ...) and a `retryable` flag. They are recognised by the
  `Z-Road-Error` response header.
* `Unreachable` means the security server you call could not be reached.
* `reply.proof` holds the signed evidence of the exchange (a dict) for live
  calls. Keep it with the decision it supported. To verify it offline use
  `zroad-cli proof-verify` or the Rust library; this package does not verify
  signatures.

### When the provider might be down

```python
message_id = zroad.submit("ZW/GOV/ZIMRA/tax-clearance/verify/v1", {"tin": "2000123456"})
# ... later, or right away:
reply = zroad.wait(message_id, max_wait=300)       # raises QueueFailed / QueueTimeout
# or check without blocking:
state = zroad.poll(message_id)                     # state.state in pending | delivered | failed
```

`submit` returns immediately; the security server stores the request on disk and
retries with backoff, safely (the message id makes retries idempotent).

## Test

```sh
PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py'
```

These tests use a scripted fake security server and need nothing else.
`tests/integration_demo.py` runs the same checks against a real network: start the
Z-Road demo with `ZROAD_DEMO_KEEP_SECS=120 scripts/demo.sh` in the Z-Road
repository and run `ZROAD_URL=http://127.0.0.1:7001 python tests/integration_demo.py`.
It passed against the demo network at the time of writing.

## Status

Early. The API follows Z-Road's internal API description
(`docs/openapi-internal.yaml` in the Z-Road repository) and may change with it.
No licence has been chosen yet.
