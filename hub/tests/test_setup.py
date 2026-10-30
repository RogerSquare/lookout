"""Setup page login: claim with the setup code, password sign-in, sessions, CSRF header, lockout,
password change, and the page itself. Run in the hub image: python tests/test_setup.py"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
PORT, RTSP, API = 18768, 18556, 18986
BASE = f"http://127.0.0.1:{PORT}"
CSRF = {"X-Lookout-CSRF": "1"}
results = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'} {name}{f' ({detail})' if detail else ''}", flush=True)
    results.append(bool(ok))


def start(env):
    return subprocess.Popen([sys.executable, str(HERE.parent / "hub.py")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def wait_up(s):
    for _ in range(150):
        try:
            async with s.get(f"{BASE}/healthz") as r:
                if r.status == 200:
                    return
        except aiohttp.ClientError:
            pass
        await asyncio.sleep(0.2)


async def main():
    data = Path(tempfile.mkdtemp())
    env = {**os.environ, "LOOKOUT_HTTPS_PORT": "18869", "LOOKOUT_BONJOUR": "0", "LOOKOUT_DATA": str(data), "LOOKOUT_CONFIG": str(data / "none.json"), "LOOKOUT_PORT": str(PORT),
           "LOOKOUT_RTSP_PORT": str(RTSP), "LOOKOUT_GO2RTC_API_PORT": str(API), "LOOKOUT_VERSION": "test"}
    proc = start(env)
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
            await wait_up(s)
            code = (data / "setup-code").read_text().strip()
            async with s.get(f"{BASE}/") as r:
                check("setup page served at /", r.status == 200 and "Lookout Hub" in await r.text())
            async with s.get(f"{BASE}/static/app.js") as r:
                check("page script served", r.status == 200 and "X-Lookout-CSRF" in await r.text())
            async with s.get(f"{BASE}/api/admin/session") as r:
                st = await r.json()
                check("new hub: not claimed, not signed in", st["claimed"] is False and st["signed_in"] is False)
            async with s.get(f"{BASE}/api/admin/config") as r:
                check("admin API refuses before sign-in", r.status == 401)
            async with s.post(f"{BASE}/api/admin/setup-qr", json={"code": "0000-0000-0000"}) as r:
                check("setup QR needs the setup code", r.status == 403)
            async with s.post(f"{BASE}/api/admin/setup-qr", json={"code": code}) as r:
                q = await r.json()
                check("setup QR carries the setup link", r.status == 200 and q["link"].endswith(f"/?setup={code}")
                      and "<svg" in q["qr_svg"])
            async with s.post(f"{BASE}/api/admin/setup", json={"code": "0000-0000-0000", "password": "longenough"}) as r:
                check("wrong setup code refused", r.status == 403)
            async with s.post(f"{BASE}/api/admin/setup", json={"code": code, "password": "short"}) as r:
                check("too-short password refused", r.status == 400)
            async with s.post(f"{BASE}/api/admin/setup", json={"code": code.lower(), "password": "harbour-light-9"}) as r:
                ok = r.status == 200 and "lookout_session" in r.cookies
                check("claim with the setup code (any case) signs you in", ok)
                if ok:
                    c = r.cookies["lookout_session"]
                    check("session cookie is HttpOnly and SameSite=Strict", c["httponly"] and c["samesite"] == "Strict")
            settings = json.loads((data / "settings.json").read_text())
            check("password stored hashed, setup code used up",
                  settings["admin_password"].startswith("scrypt$") and "harbour" not in json.dumps(settings)
                  and "setup_code" not in settings)
            async with s.post(f"{BASE}/api/admin/setup", json={"code": code, "password": "another-pass"}) as r:
                check("a hub can only be claimed once", r.status == 403)
            async with s.post(f"{BASE}/api/admin/setup-qr", json={"code": code}) as r:
                check("no setup QR once claimed", r.status == 403)
            async with s.get(f"{BASE}/api/admin/config") as r:
                check("signed-in session reads the admin API", r.status == 200)
            async with s.put(f"{BASE}/api/admin/settings", json={"detect_fps": 4}) as r:
                check("changes without the CSRF header refused", r.status == 403)
            async with s.put(f"{BASE}/api/admin/settings", json={"detect_fps": 4}, headers=CSRF) as r:
                body = await r.json()
                check("changes with the CSRF header accepted and applied live",
                      r.status == 200 and body["restart_needed"] is False)
            async with s.post(f"{BASE}/api/admin/logout", headers=CSRF) as r:
                pass
            async with s.get(f"{BASE}/api/admin/config") as r:
                check("signed out", r.status == 401)
            for _ in range(5):
                async with s.post(f"{BASE}/api/admin/login", json={"password": "wrong-wrong"}) as r:
                    pass
            async with s.post(f"{BASE}/api/admin/login", json={"password": "harbour-light-9"}) as r:
                check("five wrong passwords lock sign-in for a minute", r.status == 403 and "wait" in await r.text())
        proc.terminate()
        proc.wait(8)
        proc = start(env)
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
            await wait_up(s)
            async with s.get(f"{BASE}/api/admin/session") as r:
                check("after a restart the hub stays claimed", (await r.json())["claimed"] is True)
            async with s.post(f"{BASE}/api/admin/login", json={"password": "harbour-light-9"}) as r:
                check("password sign-in works", r.status == 200)
            async with s.post(f"{BASE}/api/admin/password", json={"current": "nope-nope", "new": "new-pass-123"}, headers=CSRF) as r:
                check("password change needs the current password", r.status == 403)
            async with s.post(f"{BASE}/api/admin/password", json={"current": "harbour-light-9", "new": "new-pass-123"}, headers=CSRF) as r:
                check("password changed", r.status == 200)
            async with s.post(f"{BASE}/api/admin/login", json={"password": "harbour-light-9"}) as r:
                check("old password no longer works", r.status == 403)
            admin = json.loads((data / "settings.json").read_text())["admin_token"]
            async with aiohttp.ClientSession() as s2:
                async with s2.get(f"{BASE}/api/admin/config", headers={"Authorization": f"Bearer {admin}"}) as r:
                    check("admin token (for scripts) still works without a session", r.status == 200)
                async with s2.get(f"{BASE}/", headers={"Cf-Connecting-Ip": "203.0.113.9"}) as r:
                    check("setup page not reachable through the internet tunnel", r.status == 404)
                A = {"Authorization": f"Bearer {admin}"}
                async with s2.put(f"{BASE}/api/admin/settings", json={"remote_url": "http://plain.example.com"}, headers=A) as r:
                    check("away address must be https", r.status == 400)
                async with s2.put(f"{BASE}/api/admin/settings", json={"remote_url": "https://lookout.example.com", "hub_name": "Cabin"},
                                  headers=A) as r:
                    check("away address and hub name saved live", r.status == 200 and not (await r.json())["restart_needed"])
                async with s2.get(f"{BASE}/api/watch/hub") as r:
                    info = await r.json()
                    check("apps see the new name and away address",
                          info["name"] == "Cabin" and info["remote_url"] == "https://lookout.example.com")
            check("setup code file removed once the hub is claimed", not (data / "setup-code").exists())
        proc.terminate()
        proc.wait(8)
        out = subprocess.run([sys.executable, str(HERE.parent / "hub.py"), "--reset-password"], env=env,
                             capture_output=True, text=True, timeout=30)
        check("--reset-password runs", out.returncode == 0 and "Password removed" in out.stdout)
        proc = start(env)
        async with aiohttp.ClientSession() as s:
            await wait_up(s)
            async with s.get(f"{BASE}/api/admin/session") as r:
                check("after a reset the hub can be claimed again", (await r.json())["claimed"] is False)
            check("and prints a new setup code", (data / "setup-code").exists())
            async with s.get(f"{BASE}/api/watch/hub") as r:
                check("settings kept through the reset", (await r.json())["name"] == "Cabin")
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
