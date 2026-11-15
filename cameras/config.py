"""Camera app settings: the go2rtc server and the UniFi Protect sites."""
import os
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


# Event kinds in display order. ``motion`` is plain motion with no smart detection.
EVENT_KINDS = ("doorbell", "person", "vehicle", "package", "animal", "motion")
EVENT_KIND_DEFAULTS = {
    "doorbell": True, "person": True, "vehicle": True,
    "package": True, "animal": False, "motion": False,
}


def enabled_event_kinds():
    """Event kinds turned on in Settings (``show_<kind>``)."""
    return [
        k for k in EVENT_KINDS
        if app_setting("cameras", f"show_{k}", EVENT_KIND_DEFAULTS[k], cast=as_bool)
    ]


def keep_open():
    """Hold off the hub screensaver while the cameras page is open."""
    return app_setting("cameras", "keep_open", True, cast=as_bool)


def video_mode():
    """go2rtc player mode for live video: ``webrtc/tcp`` (default) or ``webrtc`` (UDP first)."""
    return "webrtc" if app_setting("cameras", "video_transport", "tcp") == "udp" else "webrtc/tcp"


def popup_seconds():
    try:
        return max(10, int(app_setting("cameras", "popup_seconds", 60)))
    except (TypeError, ValueError):
        return 60