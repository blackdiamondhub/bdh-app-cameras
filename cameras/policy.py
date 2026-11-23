"""What guests (anyone who isn't a signed-in admin) may see and do.

Every view that serves camera data goes through these checks. Admins can do
everything. The rules are set on the Guests settings page and saved as app
state ``guests``::

    {"nvrs": {"<nvr_id>": {"default": "visible",
                           "cameras": {"<camera_id>": "hidden"}}},
     "talk": true,
     "ptz": true,
     "events": "off",            # or "24h", or "since"
     "events_since": "2026-05-24T18:00:00-07:00"}

A camera is visible to guests when its NVR's ``default`` (or the camera's own
override) says so. NVRs missing from ``nvrs`` are hidden. Until the page is
saved, guests see the first NVR only.
"""
from datetime import datetime, timedelta

from bdh_sdk import is_admin
from django.utils import timezone

STATE_KEY = "guests"
VISIBLE, HIDDEN = "visible", "hidden"
EVENT_MODES = ("off", "24h", "since")
RECENT = timedelta(hours=24)
# Set on the config in effect while the Guests page has never been saved.
FIRST_ONLY = "first_nvr_only"


def _state():
    from bdh_sdk.storage import app_state

    return app_state("cameras")


def _since(value):
    """An aware datetime from ISO text (naive means the hub's local time), else None."""
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value or "").strip())
        except ValueError:
            return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def clean(raw):
    """A valid guest config from ``raw`` (e.g. a settings page POST); bad parts are dropped."""
    raw = raw if isinstance(raw, dict) else {}
    nvrs = {}
    given = raw.get("nvrs")
    for nvr_id, entry in (given.items() if isinstance(given, dict) else ()):
        if not isinstance(nvr_id, str) or not nvr_id or len(nvr_id) > 100 or not isinstance(entry, dict):
            continue
        default = VISIBLE if entry.get("default") == VISIBLE else HIDDEN
        cams = entry.get("cameras")
        overrides = {
            cid: v for cid, v in (cams.items() if isinstance(cams, dict) else ())
            if isinstance(cid, str) and cid and len(cid) <= 100 and v in (VISIBLE, HIDDEN) and v != default
        }
        nvrs[nvr_id] = {"default": default, "cameras": overrides}
    events = raw.get("events") if raw.get("events") in EVENT_MODES else "off"
    since = _since(raw.get("events_since")) if raw.get("events_since") else None
    if events == "since" and since is None:
        events = "off"
    return {
        "nvrs": nvrs,
        "talk": raw.get("talk", True) is not False,
        "ptz": raw.get("ptz", True) is not False,
        "events": events,
        "events_since": since.isoformat() if since else "",
    }


def stored():
    """The config saved from the Guests page, or None if it was never saved."""
    raw = _state().get(STATE_KEY)
    return clean(raw) if isinstance(raw, dict) else None


def current():
    """The guest config in effect: saved, else the default (only the first NVR is visible)."""
    cfg = stored()
    if cfg is not None:
        return cfg
    # Not saved: the first NVR the cameras page lists, as before.
    return {**clean({}), FIRST_ONLY: True}


def save(raw):
    cfg = clean(raw)
    _state().set(STATE_KEY, cfg)
    return cfg


def camera_visible(cfg, site, index, camera_id):
    """Whether guests see ``camera_id`` on ``site`` (the ``index``-th NVR) under ``cfg``."""
    if cfg.get(FIRST_ONLY):
        return index == 0
    nvr_id = site.get("id")
    entry = None if nvr_id is None else cfg["nvrs"].get(str(nvr_id))
    if entry is None:
        return False
    return entry["cameras"].get(camera_id, entry["default"]) == VISIBLE


def guest_sites(sites, cfg=None):
    """``sites`` (from ``get_protect_cameras``) cut down to the cameras guests see."""
    cfg = cfg or current()
    out = []
    for i, site in enumerate(sites):
        cams = [c for c in site.get("cameras", []) if camera_visible(cfg, site, i, c.get("camera_id", ""))]
        if cams:
            out.append({**site, "cameras": cams})
    return out


def visible_camera_ids(cfg=None, sites=None):
    """Ids of the cameras guests see, from ``sites`` (a list or a function returning one;
    default: every NVR's cameras)."""
    if sites is None:
        from .protect_api import get_protect_cameras as sites
    if callable(sites):
        sites = sites()
    return {
        c["camera_id"] for site in guest_sites(sites, cfg)
        for c in site["cameras"] if c.get("camera_id")
    }


def can_view(request, camera_id, sites=None):
    return is_admin(request) or bool(camera_id) and camera_id in visible_camera_ids(sites=sites)


def can_talk(request):
    return is_admin(request) or current()["talk"]


def can_ptz(request):
    return is_admin(request) or current()["ptz"]


def can_see_events(request):
    return is_admin(request) or current()["events"] != "off"


def event_window(request):
    """The earliest moment ``request`` may see events and recordings from.

    None for admins (whatever is recorded) and for guests without events
    (check :func:`can_see_events` first).
    """
    if is_admin(request):
        return None
    cfg = current()
    if cfg["events"] == "24h":
        return timezone.now() - RECENT
    if cfg["events"] == "since":
        return _since(cfg["events_since"])
    return None


def can_see_moment(request, camera_id, when, sites=None):
    """Whether ``request`` may see ``camera_id``'s recording or event at ``when``."""
    if is_admin(request):
        return True
    if not can_see_events(request) or not can_view(request, camera_id, sites):
        return False
    start = event_window(request)
    return when is not None and (start is None or when >= start)
