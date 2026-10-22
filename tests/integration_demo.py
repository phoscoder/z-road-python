"""Run against a live Z-Road network, for example the one `scripts/demo.sh`
starts in the z-road repository (set ZROAD_DEMO_KEEP=1 to leave it running).

    ZROAD_URL=http://127.0.0.1:7001 python tests/integration_demo.py
"""

import os
import sys

from zroad_client import Client, ZRoadError

url = os.environ.get("ZROAD_URL", "http://127.0.0.1:7001")
svc = "ZW/GOV/ZIMRA/tax-clearance/verify/v1"
zroad = Client(url, "ZW/COM/12345678/accounts-payable")

reply = zroad.call(svc, {"tin": "2000123456"}, subject="2000123456")
assert reply.status == 200 and reply.json()["status"] == "VALID", reply
assert reply.proof and reply.message_id
print("live call ok, proof attached:", reply.message_id)

assert zroad.call(svc, {"tin": "0000000000"}).json()["status"] == "NOT_FOUND"
print("provider 404 is a reply ok")

mid = zroad.submit(svc, {"tin": "2000987654"})
assert zroad.wait(mid, max_wait=30).json()["status"] == "EXPIRED"
print("queued call ok")

try:
    Client(url, "ZW/COM/77777777/ap").call(svc, {})
except ZRoadError as e:
    assert e.code == "invalid_request", e
    print("claiming another member is rejected ok")
else:
    sys.exit("expected an error")
print("all integration checks passed")
