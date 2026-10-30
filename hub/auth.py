"""Setup-page login: a one-time setup code to claim a new hub, then a password and session cookies.

The setup code is printed in the hub's log (and kept in /data/setup-code) so whoever runs the
container - not just anyone on the network - can claim it. Passwords are stored as scrypt hashes;
sessions are random tokens kept as hashes. Cookie-authenticated changes must carry the
X-Lookout-CSRF header, which a form on another website can't add.
"""

import hashlib
import hmac
import secrets
import threading
import time

SESSION_COOKIE = "lookout_session"
SESSION_DAYS = 30
CSRF_HEADER = "X-Lookout-CSRF"
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password, stored):
    try:
        _, salt, digest = stored.split("$")
    except (ValueError, AttributeError):
        return False
    test = hash_password(password, bytes.fromhex(salt)).split("$")[2]
    return hmac.compare_digest(test, digest)


def _h(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.failures = []          # times of recent wrong passwords / setup codes
        data = store.data
        if not data.get("admin_password") and not data.get("setup_code"):
            data["setup_code"] = "-".join(secrets.token_hex(2).upper() for _ in range(3))   # e.g. 4F2A-91C3-0B7E
            store._save()

    @property
    def claimed(self):
        return bool(self.store.data.get("admin_password"))

    @property
    def setup_code(self):
        return self.store.data.get("setup_code")

    def locked_out(self):
        now = time.time()
        with self.lock:
            self.failures = [t for t in self.failures if now - t < 60]
            return len(self.failures) >= 5

    def _fail(self):
        with self.lock:
            self.failures.append(time.time())

    def claim(self, code, password):
        """First run: the setup code proves you run the hub; returns a session token."""
        if self.claimed:
            raise PermissionError("this hub already has a password")
        if self.locked_out():
            raise PermissionError("too many attempts; wait a minute")
        if not hmac.compare_digest(str(code).strip().upper(), self.setup_code or "~"):
            self._fail()
            raise PermissionError("that setup code doesn't match")
        if len(password) < 8:
            raise ValueError("use at least 8 characters")
        with self.store.lock:
            self.store.data["admin_password"] = hash_password(password)
            self.store.data.pop("setup_code", None)
        return self._new_session()

    def login(self, password):
        if self.locked_out():
            raise PermissionError("too many attempts; wait a minute")
        if not self.claimed or not check_password(password, self.store.data["admin_password"]):
            self._fail()
            raise PermissionError("wrong password")
        return self._new_session()

    def change_password(self, old, new):
        if not check_password(old, self.store.data.get("admin_password", "")):
            self._fail()
            raise PermissionError("current password is wrong")
        if len(new) < 8:
            raise ValueError("use at least 8 characters")
        with self.store.lock:
            self.store.data["admin_password"] = hash_password(new)
            self.store.data["sessions"] = {}          # sign out everywhere else
        return self._new_session()

    def _new_session(self):
        token = secrets.token_urlsafe(32)
        with self.store.lock:
            sessions = self.store.data.setdefault("sessions", {})
            now = time.time()
            for k in [k for k, exp in sessions.items() if exp < now]:
                del sessions[k]
            sessions[_h(token)] = now + SESSION_DAYS * 86400
            self.store._save()
        return token

    def valid(self, token):
        if not token:
            return False
        exp = (self.store.data.get("sessions") or {}).get(_h(token))
        return bool(exp and exp > time.time())

    def logout(self, token):
        with self.store.lock:
            (self.store.data.get("sessions") or {}).pop(_h(token or ""), None)
            self.store._save()
