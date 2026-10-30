"""Pairing apps with the hub, and checking who is asking.

Each hub has an id and its own TLS certificate (made on first start). The setup page shows a QR
code with the hub's id, addresses, certificate fingerprint and a one-time pairing code; an app
scans it, makes a P-256 key (Secure Enclave on Apple devices) and registers the public half.

From then on the app signs each request:
    Authorization: Lookout <device_id>:<unix time>:<base64 DER ECDSA-SHA256 signature>
over "<METHOD>\\n<path?query>\\n<unix time>". Requests more than 2 minutes off, or a signature seen
before, are refused. Things a player opens by URL (HLS, MJPEG, images) use a short-lived media
token from GET /api/watch/media-token instead, passed as ?token=.
"""

import base64
import datetime
import hashlib
import hmac
import secrets
import threading
import time
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

CODE_TTL = 600            # a pairing code lasts 10 minutes
MEDIA_TTL = 600           # media tokens last 10 minutes
CLOCK_SKEW = 120          # signed requests may be this far off in seconds
MAX_TRIES = 5             # wrong pairing codes per minute before pairing pauses
PLATFORMS = ("ios", "watchos", "android", "windows", "macos", "other")


class Pairing:
    def __init__(self, store, data_dir):
        self.store = store
        self.lock = threading.Lock()
        self.codes = {}               # secret -> {short, expires, kind, by}
        self.seen = {}                # recent signatures -> expiry (replay guard)
        self.misses = []              # times of wrong pairing codes
        d = store.data
        changed = False
        if not d.get("hub_id"):
            d["hub_id"] = secrets.token_hex(8)
            changed = True
        if not d.get("media_key"):
            d["media_key"] = secrets.token_hex(32)
            changed = True
        d.setdefault("devices", {})
        if changed:
            store._save()
        self.cert_file = Path(data_dir) / "tls-cert.pem"
        self.key_file = Path(data_dir) / "tls-key.pem"
        self._ensure_cert()

    # ---- identity ----------------------------------------------------------------------------

    @property
    def hub_id(self):
        return self.store.data["hub_id"]

    def _ensure_cert(self):
        if self.cert_file.exists() and self.key_file.exists():
            return
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"Lookout Hub {self.hub_id}")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=3650))
                .sign(key, hashes.SHA256()))
        self.key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                    serialization.NoEncryption()))
        self.key_file.chmod(0o600)
        self.cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    @property
    def fingerprint(self):
        """SHA-256 of the certificate (hex): what apps pin."""
        cert = x509.load_pem_x509_certificate(self.cert_file.read_bytes())
        return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()

    # ---- pairing codes -------------------------------------------------------------------------

    def new_code(self, kind="app", by=None):
        """A one-time code: a long secret for QR codes, and a 6-digit form for typing."""
        now = time.time()
        with self.lock:
            self.codes = {k: v for k, v in self.codes.items() if v["expires"] > now}
            secret = secrets.token_urlsafe(9)      # 72 bits, one use, 10 minutes: short keeps the QR easy to scan
            short = f"{secrets.randbelow(10**6):06d}"
            self.codes[secret] = {"short": short, "expires": now + CODE_TTL, "kind": kind, "by": by}
        return {"secret": secret, "code": short, "expires": now + CODE_TTL}

    def _take_code(self, given):
        now = time.time()
        with self.lock:
            for secret, c in list(self.codes.items()):
                if c["expires"] < now:
                    del self.codes[secret]
                elif hmac.compare_digest(given, secret) or hmac.compare_digest(given, c["short"]):
                    return self.codes.pop(secret)          # one use only
        return None

    def pair(self, code, name, platform, public_key_b64):
        """Register a device with a pairing code. Returns its device record."""
        if not isinstance(code, str) or not code:
            raise PermissionError("pairing code required")
        if platform not in PLATFORMS:
            raise ValueError(f"platform must be one of {PLATFORMS}")
        try:
            raw = base64.b64decode(public_key_b64, validate=True)
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
        except Exception:
            raise ValueError("public_key must be a base64 P-256 point (X9.63, 65 bytes)")
        now = time.time()
        self.misses = [t for t in self.misses if now - t < 60]
        if len(self.misses) >= MAX_TRIES:
            raise PermissionError("too many wrong codes; wait a minute and try again")
        c = self._take_code(code.strip())
        if c is None:
            self.misses.append(now)
            raise PermissionError("that pairing code is wrong or has expired")
        device_id = secrets.token_hex(8)
        rec = {"id": device_id, "name": str(name or platform)[:60], "platform": platform,
               "public_key": public_key_b64, "paired": time.time(), "last_seen": None, "via": c.get("by")}
        with self.store.lock:
            self.store.data["devices"][device_id] = rec
            self.store._save()
        return rec

    # ---- devices -------------------------------------------------------------------------------

    def devices(self):
        return sorted(({k: v for k, v in d.items() if k != "public_key"} for d in self.store.data["devices"].values()),
                      key=lambda d: d["paired"])

    def revoke(self, device_id):
        with self.store.lock:
            gone = self.store.data["devices"].pop(device_id, None)
            if gone:
                self.store._save()
        return gone is not None

    def _touch(self, rec):
        now = time.time()
        if not rec.get("last_seen") or now - rec["last_seen"] > 300:   # don't rewrite the file every request
            rec["last_seen"] = now
            with self.store.lock:
                self.store._save()

    # ---- request checks --------------------------------------------------------------------------

    def verify(self, header, method, path_qs):
        """Device record for a valid 'Lookout <id>:<ts>:<sig>' header, else None."""
        if not header or not header.startswith("Lookout "):
            return None
        try:
            device_id, ts, sig_b64 = header[8:].split(":", 2)
            ts_i = int(ts)
            sig = base64.b64decode(sig_b64, validate=True)
        except (ValueError, TypeError):
            return None
        now = time.time()
        if abs(now - ts_i) > CLOCK_SKEW:
            return None
        rec = self.store.data["devices"].get(device_id)
        if rec is None:
            return None
        key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), base64.b64decode(rec["public_key"]))
        try:
            key.verify(sig, f"{method}\n{path_qs}\n{ts}".encode(), ec.ECDSA(hashes.SHA256()))
        except InvalidSignature:
            return None
        digest = hashlib.sha256(sig).hexdigest()
        with self.lock:
            self.seen = {k: v for k, v in self.seen.items() if v > now}
            if digest in self.seen:
                return None                                   # replayed
            self.seen[digest] = now + 2 * CLOCK_SKEW
        self._touch(rec)
        return rec

    def media_token(self, device_id):
        exp = int(time.time()) + MEDIA_TTL
        body = f"{device_id}.{exp}"
        mac = hmac.new(bytes.fromhex(self.store.data["media_key"]), body.encode(), hashlib.sha256).hexdigest()[:32]
        return {"token": f"m.{body}.{mac}", "expires": exp}

    def check_media(self, token):
        """Device record for a valid, unexpired media token of a still-paired device."""
        if not token or not token.startswith("m."):
            return None
        try:
            _, device_id, exp, mac = token.split(".")
        except ValueError:
            return None
        want = hmac.new(bytes.fromhex(self.store.data["media_key"]), f"{device_id}.{exp}".encode(),
                        hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(mac, want) or int(exp) < time.time():
            return None
        return self.store.data["devices"].get(device_id)
