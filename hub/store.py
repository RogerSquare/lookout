"""Hub settings and cameras, kept in one JSON file in the data folder and changed through the
admin API instead of by hand.

A fresh hub starts with no cameras. An older hub's hand-written config.json is imported once,
on first start, keeping its app token so paired apps carry on working.
"""

import json
import re
import secrets
import threading
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

VERSION = 1

# Settings the admin API may change (cameras have their own calls; tokens are never editable)
EDITABLE = {
    "detect_fps", "confidence", "trigger_hits", "trigger_window", "hide_after_seconds", "threads",
    "model", "gpu_helper", "gpu_helper_timeout_ms", "go2rtc", "public_host",
    "detect_power", "model_families", "hub_name", "remote_url", "allow_legacy_token",
    "alerts_to", "auto_add_cameras",
}
CAMERA_FIELDS = {
    "id", "name", "role", "url", "sub_url", "detect_url", "streams", "hls_src", "hls_variants",
    "min_person_height", "zone", "loop", "enabled", "ptz", "frigate", "bridge",
}
ROLES = ("door", "other")
_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


class ConfigError(ValueError):
    pass


def mask_url(url):
    """rtsp://user:secret@host/x -> rtsp://user:***@host/x (for anything shown or logged)."""
    if not isinstance(url, str) or "@" not in url:
        return url
    parts = urlsplit(url)
    if parts.password is None:
        return url
    netloc = f"{parts.username}:***@{parts.hostname}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit(parts._replace(netloc=netloc))


def validate_camera(cam):
    """Checks one camera and returns a clean copy (unknown keys dropped)."""
    if not isinstance(cam, dict):
        raise ConfigError("camera must be an object")
    cam = {k: v for k, v in cam.items() if k in CAMERA_FIELDS}
    cid = cam.get("id", "")
    if not isinstance(cid, str) or not _ID.fullmatch(cid):
        raise ConfigError("id must be 1-32 chars of a-z, 0-9, _ or -, starting with a letter or digit")
    if not (cam.get("url") or cam.get("detect_url")):
        raise ConfigError("camera needs a url (its stream address)")
    for key in ("url", "sub_url", "detect_url"):
        if key in cam and not isinstance(cam[key], str):
            raise ConfigError(f"{key} must be a string")
    cam.setdefault("name", cid)
    cam.setdefault("role", "other")
    if cam["role"] not in ROLES:
        raise ConfigError(f"role must be one of {ROLES}")
    h = cam.get("min_person_height", 0.0)
    if not isinstance(h, (int, float)) or not 0 <= h < 1:
        raise ConfigError("min_person_height must be a fraction of the frame height, 0 to 1")
    zone = cam.get("zone")
    if zone is not None:
        ok = isinstance(zone, list) and len(zone) >= 3 and all(
            isinstance(p, list) and len(p) == 2 and all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in p)
            for p in zone)
        if not ok:
            raise ConfigError("zone must be 3+ [x, y] points, each a 0-1 fraction of the frame")
    if "bridge" in cam and not (isinstance(cam["bridge"], str) and cam["bridge"].strip()):
        raise ConfigError("bridge must be the camera's stream name in the camera bridge")
    if "frigate" in cam and not (isinstance(cam["frigate"], str) and _ID.fullmatch(cam["frigate"])):
        raise ConfigError("frigate must be the camera's name in Frigate")
    if cam.get("ptz") is not None:
        import ptz
        try:
            cam["ptz"] = ptz.validate(cam["ptz"])
        except ValueError as e:
            raise ConfigError(str(e))
    else:
        cam.pop("ptz", None)
    cam.setdefault("enabled", True)
    return cam


def mask_camera(cam):
    """A camera as the setup page sees it: stream passwords and the PTZ key masked."""
    c = dict(cam)
    for key in ("url", "sub_url", "detect_url"):
        if key in c:
            c[key] = mask_url(c[key])
    if c.get("ptz", {}).get("api_key"):
        c["ptz"] = {**c["ptz"], "api_key": "***"}
    return c


class Store:
    def __init__(self, path, legacy_config=None):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.imported = False
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {"version": VERSION, "settings": {}, "cameras": []}
            if legacy_config and Path(legacy_config).exists():
                self._import(json.loads(Path(legacy_config).read_text(encoding="utf-8")))
        fresh = not self.path.exists() and not self.imported
        if fresh:
            self.data["settings"]["allow_legacy_token"] = False    # new hubs only take paired apps
        changed = self.path.exists() is False or self.imported
        # Secrets made once and kept: the app token (until per-device pairing) and the admin token
        if not self.data.get("app_token"):
            self.data["app_token"] = secrets.token_urlsafe(24)
            changed = True
        if not self.data.get("admin_token"):
            self.data["admin_token"] = secrets.token_urlsafe(24)
            changed = True
        if changed:
            self._save()

    def _import(self, legacy):
        cams = []
        for c in legacy.get("cameras", []):
            # Old configs had no roles; their alert wording treated "kitchen" as the front door
            c = {**c, "role": c.get("role") or ("door" if c.get("id") == "kitchen" else "other")}
            cams.append(validate_camera(c))
        self.data["cameras"] = cams
        self.data["settings"] = {k: v for k, v in legacy.items() if k not in ("cameras", "watch_token")}
        if legacy.get("watch_token"):
            self.data["app_token"] = legacy["watch_token"]
        # An old hub reads its streams from a go2rtc it doesn't run; keep it that way until the
        # cameras are switched over to the built-in one
        self.data["settings"].setdefault("go2rtc", "off")
        self.imported = True

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        try:
            tmp.chmod(0o600)   # holds camera passwords and tokens
        except OSError:
            pass
        tmp.replace(self.path)

    # ---- reads ---------------------------------------------------------------------------

    @property
    def app_token(self):
        return self.data["app_token"]

    @property
    def admin_token(self):
        return self.data["admin_token"]

    def settings(self, defaults):
        s = dict(defaults)
        s.update(self.data["settings"])
        s["watch_token"] = self.app_token
        return s

    def source(self):
        return self.data.get("source")

    def set_source(self, cfg):
        with self.lock:
            self.data["source"] = cfg
            self._save()

    def dismissed(self):
        """Found cameras (Frigate names) the user removed or dismissed: never offered again."""
        return set(self.data.get("dismissed_cameras") or [])

    def set_dismissed(self, names):
        with self.lock:
            self.data["dismissed_cameras"] = sorted(names)
            self._save()

    def bridge(self):
        """The camera bridge that sends commands (wyze-bridge 3.x): {url, api_key?} or None."""
        return self.data.get("bridge")

    def set_bridge(self, cfg):
        with self.lock:
            if cfg:
                self.data["bridge"] = cfg
            else:
                self.data.pop("bridge", None)
            self._save()

    def history(self):
        """Frigate used for event history and clips while the hub detects itself (None = off)."""
        return self.data.get("history")

    def set_history(self, cfg):
        with self.lock:
            if cfg:
                self.data["history"] = cfg
            else:
                self.data.pop("history", None)
            self._save()

    def profile(self):
        return self.data.get("profile")

    def set_profile(self, prof):
        with self.lock:
            self.data["profile"] = prof
            self._save()

    def cameras(self):
        with self.lock:
            return [dict(c) for c in self.data["cameras"] if c.get("enabled", True)]

    def public(self):
        """Everything for the setup page, with stream passwords masked and tokens left out."""
        with self.lock:
            cams = []
            for c in self.data["cameras"]:
                cams.append(mask_camera(c))
            return {"version": self.data["version"], "settings": dict(self.data["settings"]), "cameras": cams}

    # ---- writes --------------------------------------------------------------------------

    def put_camera(self, cam):
        """Add a camera, or replace the one with the same id. A masked password ("***") in the
        new url keeps the stored one, so the setup page can save without re-entering it."""
        keep_key = isinstance(cam, dict) and isinstance(cam.get("ptz"), dict) and cam["ptz"].get("api_key") == "***"
        cam = validate_camera(cam)
        with self.lock:
            cams = self.data["cameras"]
            old = next((c for c in cams if c["id"] == cam["id"]), None)
            if old:
                for key in ("url", "sub_url", "detect_url"):
                    if key in cam and ":***@" in cam[key] and old.get(key) and mask_url(old[key]) == cam[key]:
                        cam[key] = old[key]
                if keep_key and cam.get("ptz"):
                    cam["ptz"]["api_key"] = (old.get("ptz") or {}).get("api_key")
                    if not cam["ptz"]["api_key"]:
                        cam["ptz"].pop("api_key")
                cams[cams.index(old)] = cam
            else:
                cams.append(cam)
            self._save()
        return cam

    def delete_camera(self, cam_id):
        with self.lock:
            before = len(self.data["cameras"])
            self.data["cameras"] = [c for c in self.data["cameras"] if c["id"] != cam_id]
            if len(self.data["cameras"]) == before:
                return False
            self._save()
            return True

    def update_settings(self, patch):
        bad = set(patch) - EDITABLE
        if bad:
            raise ConfigError(f"not editable: {', '.join(sorted(bad))}")
        if "detect_power" in patch and patch["detect_power"] not in ("power", "balanced", "accuracy"):
            raise ConfigError("detect_power must be power, balanced or accuracy")
        if "hub_name" in patch and not (isinstance(patch["hub_name"], str) and 0 < len(patch["hub_name"].strip()) <= 40):
            raise ConfigError("hub_name must be 1-40 characters")
        if "auto_add_cameras" in patch and not isinstance(patch["auto_add_cameras"], bool):
            raise ConfigError("auto_add_cameras must be true or false")
        if "alerts_to" in patch and patch["alerts_to"] not in ("phone", "watch", "all"):
            raise ConfigError("alerts_to must be phone, watch or all")
        remote = patch.get("remote_url")
        if remote is not None and not (isinstance(remote, str) and re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d+)?", remote)):
            raise ConfigError("remote_url must be an https:// address with no path, e.g. https://lookout.example.com")
        with self.lock:
            self.data["settings"].update(patch)
            self._save()
        return dict(self.data["settings"])
