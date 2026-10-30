"""Pairing apps: hub identity, one-time codes, signed requests, media tokens, companion (watch)
pairing, revoke, the pinned HTTPS port and the old shared token switch.
Run in the hub image: python tests/test_pairing.py"""

import asyncio
import base64
import hashlib
import json
import os
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

HERE = Path(__file__).resolve().parent
PORT, HTTPS, RTSP, API = 18770, 18771, 18557, 18987
BASE = f"http://127.0.0.1:{PORT}"
results = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'} {name}{f' ({detail})' if detail else ''}", flush=True)
    results.append(bool(ok))


class Device:
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.id = None

    @property
    def public(self):
        raw = self.key.public_key().public_bytes(serialization.Encoding.X962,
                                                 serialization.PublicFormat.UncompressedPoint)
        return base64.b64encode(raw).decode()

    def sign(self, method, path_qs, ts=None):
        ts = str(int(ts if ts is not None else time.time()))
        sig = self.key.sign(f"{method}\n{path_qs}\n{ts}".encode(), ec.ECDSA(hashes.SHA256()))
        return {"Authorization": f"Lookout {self.id}:{ts}:{base64.b64encode(sig).decode()}"}


async def main():
    data = Path(tempfile.mkdtemp())
    env = {**os.environ, "LOOKOUT_DATA": str(data), "LOOKOUT_CONFIG": str(data / "none.json"), "LOOKOUT_PORT": str(PORT),
           "LOOKOUT_HTTPS_PORT": str(HTTPS), "LOOKOUT_RTSP_PORT": str(RTSP), "LOOKOUT_GO2RTC_API_PORT": str(API),
           "LOOKOUT_VERSION": "test", "LOOKOUT_BONJOUR": "0"}
    proc = subprocess.Popen([sys.executable, str(HERE.parent / "hub.py")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        async with aiohttp.ClientSession() as s:
            for _ in range(150):
                try:
                    async with s.get(f"{BASE}/healthz") as r:
                        if r.status == 200:
                            break
                except aiohttp.ClientError:
                    pass
                await asyncio.sleep(0.2)
            admin = {"Authorization": "Bearer " + (data / "admin-token").read_text().strip()}
            app_token = json.loads((data / "settings.json").read_text())["app_token"]

            async with s.get(f"{BASE}/api/watch/hub") as r:
                info = await r.json()
                check("hub identity is open (id, name, fingerprint)",
                      r.status == 200 and len(info["hub_id"]) == 16 and len(info["fingerprint"]) == 64)
            async with s.get(f"{BASE}/api/watch/cameras") as r:
                check("app API refuses an unpaired caller", r.status == 401)
            async with s.get(f"{BASE}/api/watch/cameras", headers={"Authorization": f"Bearer {app_token}"}) as r:
                check("a new hub refuses the old shared token", r.status == 401)

            async with s.post(f"{BASE}/api/admin/pairing", headers=admin) as r:
                code = await r.json()
                check("setup page makes a pairing code with a QR",
                      r.status == 200 and len(code["code"]) == 6 and "<svg" in code["qr_svg"]
                      and code["link"].startswith("lookout://pair?") and info["fingerprint"] in code["link"])
            phone = Device()
            async with s.post(f"{BASE}/api/watch/pair", json={"code": "000000x", "name": "x", "platform": "ios",
                                                              "public_key": phone.public}) as r:
                check("wrong pairing code refused", r.status == 403)
            async with s.post(f"{BASE}/api/watch/pair", json={"code": code["code"], "name": "x", "platform": "ios",
                                                              "public_key": "bm9wZQ=="}) as r:
                check("a bad public key is refused", r.status == 400)
            async with s.post(f"{BASE}/api/watch/pair", json={"code": code["code"], "name": "x", "platform": "ios",
                                                              "public_key": phone.public},
                              headers={"Cf-Connecting-Ip": "203.0.113.9"}) as r:
                check("the 6-digit code doesn't work from the internet", r.status == 403)
            async with s.post(f"{BASE}/api/watch/pair", json={"code": code["code"], "name": "Roger's iPhone",
                                                              "platform": "ios", "public_key": phone.public}) as r:
                body = await r.json()
                phone.id = body.get("device_id")
                check("pair with the 6-digit code", r.status == 200 and phone.id and body["hub"]["hub_id"] == info["hub_id"])
            async with s.post(f"{BASE}/api/watch/pair", json={"code": code["code"], "name": "again", "platform": "ios",
                                                              "public_key": Device().public}) as r:
                check("a pairing code works once", r.status == 403)

            path = "/api/watch/cameras"
            h = phone.sign("GET", path)
            async with s.get(BASE + path, headers=h) as r:
                check("signed request accepted", r.status == 200)
            async with s.get(BASE + path, headers=h) as r:
                check("the same signature can't be replayed", r.status == 401)
            async with s.get(BASE + path + "?x=1", headers=phone.sign("GET", path)) as r:
                check("signature covers the query", r.status == 401)
            async with s.get(BASE + path, headers=phone.sign("GET", path, time.time() - 600)) as r:
                check("a 10-minute-old signature is refused", r.status == 401)
            async with s.get(BASE + path, headers=Device().sign("GET", path) | {}) as r:
                check("unknown device refused", r.status == 401)

            async with s.get(f"{BASE}/api/watch/media-token", headers=phone.sign("GET", "/api/watch/media-token")) as r:
                media = await r.json()
                check("paired device gets a media token", r.status == 200 and media["token"].startswith("m."))
            async with s.get(f"{BASE}{path}?token={media['token']}") as r:
                check("media token works in a URL", r.status == 200)
            forged = media["token"][:-4] + "0000"
            async with s.get(f"{BASE}{path}?token={forged}") as r:
                check("forged media token refused", r.status == 401)

            async with s.post(f"{BASE}/api/watch/pairing-code", headers=phone.sign("POST", "/api/watch/pairing-code")) as r:
                comp = await r.json()
                check("phone gets a code for its watch", r.status == 200 and comp["secret"])
            watch = Device()
            async with s.post(f"{BASE}/api/watch/pair", json={"code": comp["secret"], "name": "Ultra", "platform": "watchos",
                                                              "public_key": watch.public},
                              headers={"Cf-Connecting-Ip": "203.0.113.9"}) as r:
                watch.id = (await r.json()).get("device_id")
                check("watch pairs with the long code, even from the internet", r.status == 200 and watch.id)
            async with s.get(BASE + path, headers=watch.sign("GET", path)) as r:
                check("watch signed request accepted", r.status == 200)

            async with s.get(f"{BASE}/api/admin/devices", headers=admin) as r:
                devs = (await r.json())["devices"]
                check("setup page lists both devices, without their keys",
                      [d["platform"] for d in devs] == ["ios", "watchos"] and devs[1]["via"] == phone.id
                      and not any("public_key" in d for d in devs))
            async with s.delete(f"{BASE}/api/admin/devices/{phone.id}", headers=admin) as r:
                check("remove the phone", r.status == 200)
            async with s.get(BASE + path, headers=phone.sign("GET", path)) as r:
                check("removed device refused", r.status == 401)
            async with s.get(f"{BASE}{path}?token={media['token']}") as r:
                check("removed device's media token refused", r.status == 401)
            async with s.get(BASE + path, headers=watch.sign("GET", path)) as r:
                check("the watch keeps working", r.status == 200)

            async with s.post(f"{BASE}/api/watch/pairing-code", headers=watch.sign("POST", "/api/watch/pairing-code")) as r:
                spare = await r.json()
            extra = Device()
            async with s.post(f"{BASE}/api/watch/pair", json={"code": spare["secret"], "name": "spare", "platform": "other",
                                                              "public_key": extra.public}) as r:
                extra.id = (await r.json())["device_id"]
            async with s.post(f"{BASE}/api/watch/unpair", headers=extra.sign("POST", "/api/watch/unpair")) as r:
                check("a device can unpair itself", r.status == 200)
            async with s.get(BASE + path, headers=extra.sign("GET", path)) as r:
                check("and is refused afterwards", r.status == 401)

            for _ in range(5):
                async with s.post(f"{BASE}/api/watch/pair", json={"code": "123456", "name": "x", "platform": "ios",
                                                                  "public_key": phone.public}) as r:
                    pass
            async with s.post(f"{BASE}/api/admin/pairing", headers=admin) as r:
                fresh = await r.json()
            async with s.post(f"{BASE}/api/watch/pair", json={"code": fresh["code"], "name": "x", "platform": "ios",
                                                              "public_key": phone.public}) as r:
                check("five wrong codes pause pairing for a minute", r.status == 403 and "wait" in await r.text())

            async with s.put(f"{BASE}/api/admin/settings", json={"allow_legacy_token": True}, headers=admin) as r:
                check("old shared token can be switched on live", r.status == 200 and not (await r.json())["restart_needed"])
            async with s.get(f"{BASE}{path}", headers={"Authorization": f"Bearer {app_token}"}) as r:
                check("old shared token then works", r.status == 200)

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        reader, writer = await asyncio.open_connection("127.0.0.1", HTTPS, ssl=ctx)
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        writer.close()
        check("HTTPS port serves the certificate the QR pins", hashlib.sha256(der).hexdigest() == info["fingerprint"])
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://127.0.0.1:{HTTPS}/api/watch/hub", ssl=ctx) as r:
                check("app API over HTTPS", r.status == 200 and (await r.json())["hub_id"] == info["hub_id"])

        proc.terminate()
        proc.wait(8)
        proc = subprocess.Popen([sys.executable, str(HERE.parent / "hub.py")], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        async with aiohttp.ClientSession() as s:
            for _ in range(150):
                try:
                    async with s.get(f"{BASE}/api/watch/hub") as r:
                        again = await r.json()
                        break
                except aiohttp.ClientError:
                    await asyncio.sleep(0.2)
            check("identity and certificate survive a restart",
                  again["hub_id"] == info["hub_id"] and again["fingerprint"] == info["fingerprint"])
            async with s.get(BASE + path, headers=watch.sign("GET", path)) as r:
                check("paired watch still works after a restart", r.status == 200)
        st = (data / "tls-key.pem").stat().st_mode & 0o777
        check("TLS key readable only by the hub", st == 0o600, oct(st))
    finally:
        proc.terminate()
        try:
            proc.wait(8)
        except subprocess.TimeoutExpired:
            proc.kill()
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
