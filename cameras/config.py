"""Camera app settings: the go2rtc server and the UniFi Protect sites."""
import os
import re
import threading
import time

from bdh_sdk.settings import app_setting, as_bool


def go2rtc_url():
    """go2rtc API URL.

    In a container the supervisor's go2rtc companion (``BDH_GO2RTC_URL``) wins;
    otherwise BDH_CAMERAS_GO2RTC_URL, legacy GO2RTC_URL.
    """
    return os.environ.get("BDH_GO2RTC_URL") or app_setting(
        "cameras", "go2rtc_url", "http://localhost:1984", legacy=("GO2RTC_URL",))


NVRS_KEY = "nvrs"
SITES_TTL = 60

_sites_cache = None  # (monotonic expiry, sites)
_sites_lock = threading.Lock()


def api_key_secret(nvr_id):
    return f"protect_api_key_{nvr_id}"


def password_secret(nvr_id):
    return f"protect_password_{nvr_id}"


def nvrs():
    """The UniFi Protect consoles (NVRs) set up on the NVRs settings page.

    A list of ``{id, name, host, username}``; the API key and password are
    secrets.
    """
    from bdh_sdk.storage import app_state

    raw = app_state("cameras").get(NVRS_KEY) or []
    return [n for n in raw if isinstance(n, dict) and n.get("id") and n.get("host")]


def save_nvrs(items):
    """Store the NVR list (secrets are set separately) and drop the cached sites."""
    from bdh_sdk.storage import app_state

    app_state("cameras").set(NVRS_KEY, [
        {"id": n["id"], "name": n["name"], "host": n["host"], "username": n.get("username", "")}
        for n in items
    ])
    forget_sites()


def forget_sites():
    global _sites_cache
    with _sites_lock:
        _sites_cache = None


def protect_sites():
    """UniFi Protect sites as dicts with id, host, api_key, name, username and password.

    From the NVRs settings page, the only source. A site needs both a host and
    an API key; the username and password are an optional local Protect
    account (view only) for events and recordings. Cached for a minute, since
    secret reads are rate limited.
    """
    global _sites_cache
    with _sites_lock:
        if _sites_cache and _sites_cache[0] > time.monotonic():
            return [dict(s) for s in _sites_cache[1]]
    from bdh_sdk import secrets

    sites = []
    for n in nvrs():
        api_key = secrets.get(api_key_secret(n["id"]), "", app="cameras")
        if not api_key:
            continue
        sites.append({
            "id": n["id"],
            "host": n["host"],
            "api_key": api_key,
            "name": n.get("name") or n["host"],
            "username": n.get("username", ""),
            "password": secrets.get(password_secret(n["id"]), "", app="cameras") if n.get("username") else "",
        })
    with _sites_lock:
        _sites_cache = (time.monotonic() + SITES_TTL, sites)
    return [dict(s) for s in sites]


# Event kinds in display order: (label, group, shown by default).
# ``motion`` is plain motion with no smart detection; "heard" kinds are Protect
# audio detections.
KINDS = {
    "doorbell": ("Doorbell ring", "seen", True),
    "person": ("Person", "seen", True),
    "vehicle": ("Vehicle", "seen", True),
    "package": ("Package", "seen", True),
    "animal": ("Animal", "seen", False),
    "motion": ("Motion only", "seen", False),
    "smoke": ("Smoke alarm", "heard", False),
    "co": ("CO alarm", "heard", False),
    "baby": ("Baby crying", "heard", False),
    "speaking": ("Speaking", "heard", False),
}
EVENT_KINDS = tuple(KINDS)
EVENT_KIND_DEFAULTS = {k: v[2] for k, v in KINDS.items()}
KIND_LABELS = {k: v[0] for k, v in KINDS.items()}
# Shorter names for chips and event rows on the cameras page.
KIND_SHORT = {**{k: v.split()[0] for k, v in KIND_LABELS.items()}, "baby": "Baby crying"}

# Protect smart-detect types (objects and audio) -> our kinds.
PROTECT_KINDS = {
    "person": ("person",), "face": ("person",),
    "vehicle": ("vehicle",), "licensePlate": ("vehicle",), "car": ("vehicle",),
    "package": ("package",),
    "animal": ("animal",), "pet": ("animal",),
    "alrmSmoke": ("smoke",), "alrmCmonx": ("co",), "smoke_cmonx": ("smoke", "co"),
    "alrmBabyCry": ("baby",), "alrmSpeak": ("speaking",),
}


def protect_kinds(types):
    """Our kinds for Protect smart-detect types, in display order."""
    found = {k for t in types or () for k in PROTECT_KINDS.get(t, ())}
    return [k for k in EVENT_KINDS if k in found]


SETTINGS_KEY = "settings"
DETECTIONS_KEY = "detections"


def _state():
    from bdh_sdk.storage import app_state

    return app_state("cameras")


def settings():
    """The main settings page's values (app state); missing keys use the old flat settings."""
    raw = _state().get(SETTINGS_KEY)
    return raw if isinstance(raw, dict) else {}


def save_settings(**changes):
    _state().set(SETTINGS_KEY, {**settings(), **changes})


def enabled_event_kinds():
    """Event kinds picked under "Show these events" (before that, the old ``show_<kind>``)."""
    picked = settings().get("events")
    if isinstance(picked, list):
        return [k for k in EVENT_KINDS if k in picked]
    return [
        k for k in EVENT_KINDS
        if app_setting("cameras", f"show_{k}", EVENT_KIND_DEFAULTS[k], cast=as_bool)
    ]


def keep_open():
    """Hold off the hub screensaver while the cameras page is open."""
    value = settings().get("keep_open")
    if isinstance(value, bool):
        return value
    return app_setting("cameras", "keep_open", True, cast=as_bool)


def video_transport():
    value = settings().get("video_transport")
    if value in ("tcp", "udp"):
        return value
    return "udp" if app_setting("cameras", "video_transport", "tcp") == "udp" else "tcp"


def video_mode():
    """go2rtc player mode for live video: ``webrtc/tcp`` (default) or ``webrtc`` (UDP first)."""
    return "webrtc" if video_transport() == "udp" else "webrtc/tcp"


_AEC_URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-\[\]:]+:\d{1,5}$")


def valid_aec_url(value):
    return value == "" or bool(_AEC_URL_RE.match(value))


def aec_url():
    """The echo-cancellation sidecar (bdh-aec) base URL, or "" when it's off.

    When set, talk goes through the sidecar (it keeps what's said as the echo
    reference) and two-way cameras are heard through ``<stream>_aec``.
    """
    value = settings().get("aec_url")
    return value.rstrip("/") if isinstance(value, str) and valid_aec_url(value.rstrip("/")) else ""


POPUP_SECONDS_MIN, POPUP_SECONDS_MAX = 10, 600


def popup_seconds():
    """How long a pop-up stays up: the Pop-ups page's ``seconds``, else the old flat setting."""
    raw = _state().get("popups")
    value = raw.get("seconds") if isinstance(raw, dict) else None
    if value is None:
        value = app_setting("cameras", "popup_seconds", 60)
    try:
        return min(POPUP_SECONDS_MAX, max(POPUP_SECONDS_MIN, int(value)))
    except (TypeError, ValueError):
        return 60


def known_detections():
    """Last-known detections per NVR: ``{nvr_id: [{id, name, doorbell, enabled, supported}]}``.

    Kept so the settings pages still work while an NVR is unreachable.
    """
    raw = _state().get(DETECTIONS_KEY)
    return raw if isinstance(raw, dict) else {}


def remember_detections(nvr_id, cameras):
    """Store an NVR's cameras' detections (only when they changed)."""
    rows = [
        {"id": c["camera_id"], "name": c.get("name") or c["camera_id"],
         "doorbell": bool(c.get("is_doorbell")),
         "enabled": list(c["detections"].get("enabled", [])),
         "supported": list(c["detections"].get("supported", []))}
        for c in cameras if c.get("camera_id") and isinstance(c.get("detections"), dict)
    ]
    known = known_detections()
    if known.get(nvr_id) == rows:
        return
    _state().set(DETECTIONS_KEY, {**known, nvr_id: rows})