#!/usr/bin/env python3
"""Minimal App Store Connect API client (stdlib + the system openssl), for headless signing setup.

Reads ~/.appstoreconnect/config:  KEY_ID=...  ISSUER_ID=...  KEY_PATH=/path/AuthKey_XXXX.p8

    asc.py check                         token works? lists apps
    asc.py cert DEVELOPMENT|DISTRIBUTION <csr> <out.cer>   create a certificate from a CSR
    asc.py devices                       registered devices
    asc.py register <name> <udid> [IOS]  register a device
"""

import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.appstoreconnect.apple.com/v1"


def config():
    cfg = {}
    for line in (Path.home() / ".appstoreconnect/config").read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _der_to_raw(der):
    """ECDSA DER signature -> 64-byte r||s, as JWT ES256 wants."""
    def read_int(buf, i):
        assert buf[i] == 0x02
        n = buf[i + 1]
        return buf[i + 2:i + 2 + n], i + 2 + n
    i = 2 if der[1] < 0x80 else 3
    r, i = read_int(der, i)
    s, _ = read_int(der, i)
    return r.lstrip(b"\0").rjust(32, b"\0") + s.lstrip(b"\0").rjust(32, b"\0")


def token(cfg):
    header = {"alg": "ES256", "kid": cfg["KEY_ID"], "typ": "JWT"}
    now = int(time.time())
    claims = {"iss": cfg["ISSUER_ID"], "iat": now, "exp": now + 15 * 60, "aud": "appstoreconnect-v1"}
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    der = subprocess.run(["openssl", "dgst", "-sha256", "-sign", cfg["KEY_PATH"]],
                         input=signing_input.encode(), capture_output=True, check=True).stdout
    return f"{signing_input}.{_b64(_der_to_raw(der))}"


def call(method, path, body=None):
    cfg = config()
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token(cfg)}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {path}: HTTP {e.code}\n{e.read().decode()[:800]}")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "check"
    if cmd == "check":
        apps = call("GET", "/apps?limit=50&fields[apps]=name,bundleId")["data"]
        print("token OK;", len(apps), "app(s):", ", ".join(f"{a['attributes']['name']} ({a['attributes']['bundleId']})" for a in apps))
    elif cmd == "cert":
        kind, csr, out = argv[2], Path(argv[3]).read_text(), argv[4]
        csr_body = "".join(l for l in csr.splitlines() if "CERTIFICATE REQUEST" not in l)
        data = call("POST", "/certificates", {"data": {"type": "certificates", "attributes": {
            "certificateType": kind, "csrContent": csr_body}}})["data"]
        Path(out).write_bytes(base64.b64decode(data["attributes"]["certificateContent"]))
        print(f"created {kind} certificate {data['id']} ({data['attributes']['name']}) -> {out}")
    elif cmd == "devices":
        for d in call("GET", "/devices?limit=200")["data"]:
            a = d["attributes"]
            print(f"{a['name']:30} {a['udid']:42} {a['deviceClass']:12} {a['status']}")
    elif cmd == "register":
        name, udid = argv[2], argv[3]
        platform = argv[4] if len(argv) > 4 else "IOS"
        d = call("POST", "/devices", {"data": {"type": "devices", "attributes": {
            "name": name, "udid": udid, "platform": platform}}})["data"]
        print("registered", d["attributes"]["name"], d["attributes"]["udid"])
    elif cmd == "profile":
        # profile IOS_APP_STORE|IOS_APP_DEVELOPMENT <bundle-id> <cert-id> <out.mobileprovision>
        kind, ident, cert_id, out = argv[2:6]
        found = call("GET", f"/bundleIds?filter[identifier]={ident}&limit=5")["data"]
        found = [b for b in found if b["attributes"]["identifier"] == ident]
        if found:
            bid = found[0]["id"]
        else:
            bid = call("POST", "/bundleIds", {"data": {"type": "bundleIds", "attributes": {
                "identifier": ident, "name": ident.replace(".", " "), "platform": "IOS"}}})["data"]["id"]
            print("registered bundle id", ident)
        rel = {"bundleId": {"data": {"type": "bundleIds", "id": bid}},
               "certificates": {"data": [{"type": "certificates", "id": cert_id}]}}
        if kind.endswith("DEVELOPMENT"):
            devs = call("GET", "/devices?limit=200&filter[status]=ENABLED")["data"]
            rel["devices"] = {"data": [{"type": "devices", "id": d["id"]} for d in devs]}
        name = f"Lookout {kind} {ident} {int(time.time())}"
        data = call("POST", "/profiles", {"data": {"type": "profiles", "attributes": {
            "name": name, "profileType": kind}, "relationships": rel}})["data"]
        Path(out).write_bytes(base64.b64decode(data["attributes"]["profileContent"]))
        print(f"created profile {data['attributes']['uuid']} ({name}) -> {out}")
    elif cmd == "builds":
        # builds <bundle-id>: latest uploads and their processing state
        app = call("GET", f"/apps?filter[bundleId]={argv[2]}")["data"][0]
        for b in call("GET", f"/builds?filter[app]={app['id']}&sort=-uploadedDate&limit=5")["data"]:
            a = b["attributes"]
            print(f"{a['version']:14} {a['processingState']:12} uploaded {a['uploadedDate']}  expires {a.get('expirationDate')}")
    elif cmd == "testflight":
        # testflight <bundle-id>: an internal group that gets every build, with every team user in it
        app = call("GET", f"/apps?filter[bundleId]={argv[2]}")["data"][0]
        groups = call("GET", f"/apps/{app['id']}/betaGroups")["data"]
        group = next((g for g in groups if g["attributes"]["isInternalGroup"]), None)
        if group is None:
            group = call("POST", "/betaGroups", {"data": {"type": "betaGroups", "attributes": {
                "name": "Me", "isInternalGroup": True, "hasAccessToAllBuilds": True},
                "relationships": {"app": {"data": {"type": "apps", "id": app["id"]}}}}})["data"]
            print("created internal group", group["attributes"]["name"])
        users = call("GET", "/users?limit=50")["data"]
        for u in users:
            body = {"data": {"type": "betaTesters", "attributes": {
                "email": u["attributes"]["username"], "firstName": u["attributes"].get("firstName") or "",
                "lastName": u["attributes"].get("lastName") or ""},
                "relationships": {"betaGroups": {"data": [{"type": "betaGroups", "id": group["id"]}]}}}}
            try:
                call("POST", "/betaTesters", body)
            except SystemExit as e:
                if "409" not in str(e) and "already" not in str(e).lower():
                    raise
            print("tester:", u["attributes"]["username"])
    elif cmd == "capability":
        # capability <bundle-id> <TYPE>: e.g. PUSH_NOTIFICATIONS; no-op if already enabled
        ident, ctype = argv[2], argv[3]
        bid = [b for b in call("GET", f"/bundleIds?filter[identifier]={ident}&limit=5")["data"]
               if b["attributes"]["identifier"] == ident][0]["id"]
        have = [c["attributes"]["capabilityType"]
                for c in call("GET", f"/bundleIds/{bid}/bundleIdCapabilities")["data"]]
        if ctype in have:
            print(ident, ctype, "already enabled")
        else:
            call("POST", "/bundleIdCapabilities", {"data": {"type": "bundleIdCapabilities",
                 "attributes": {"capabilityType": ctype},
                 "relationships": {"bundleId": {"data": {"type": "bundleIds", "id": bid}}}}})
            print(ident, ctype, "enabled")
    elif cmd == "certs":
        for c in call("GET", "/certificates?limit=50")["data"]:
            a = c["attributes"]
            print(f"{c['id']:12} {a['certificateType']:14} {a['name']:45} {a.get('expirationDate', '')}")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
