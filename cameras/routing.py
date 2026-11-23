"""Websockets for live video and talk, relayed to go2rtc and aec on loopback.

go2rtc and aec only listen on 127.0.0.1, so browsers and kiosks go through
these routes, which check the guest rules (:mod:`cameras.policy`) first:

- ``/cameras/api/ws?src=<stream>``: go2rtc's player websocket, for streams of
  cameras the viewer may see.
- ``/api/talk?src=<token>``: the kiosk's talk websocket. The kiosk app
  (``BDHDisplay.talkStart``) connects without the viewer's cookies, so
  ``talk_prepare`` checks the viewer and hands out a single-use token for one
  camera's ``<stream>_talk``, good for :data:`TALK_TTL` seconds.
"""
import secrets
import threading
import time
import urllib.parse

from asgiref.sync import sync_to_async
from bdh_sdk import wsrelay
from django.db import close_old_connections, connection
from django.urls import path

from . import config, policy, views
from .views import WATCH_PATH

TALK_PATH = "/api/talk"
TALK_TTL = 30
FORBIDDEN = 4403

_talks = {}
_talks_lock = threading.Lock()


def issue_talk(stream):
    """A single-use token that lets the next talk websocket reach ``stream``."""
    token = "tk" + secrets.token_hex(16)
    now = time.monotonic()
    with _talks_lock:
        for key in [k for k, (_, at) in _talks.items() if now - at > TALK_TTL]:
            del _talks[key]
        _talks[token] = (stream, now)
    return token


def redeem_talk(token):
    """The stream ``token`` was issued for, once; None if unknown, used or expired."""
    with _talks_lock:
        entry = _talks.pop(token or "", None)
    if entry is None or time.monotonic() - entry[1] > TALK_TTL:
        return None
    return entry[0]


def _db(fn):
    """Run ``fn`` off the event loop, closing stale DB connections like a request does.

    Inside a test's transaction the connection is left alone.
    """
    def run(*args):
        if not connection.in_atomic_block:
            close_old_connections()
        try:
            return fn(*args)
        finally:
            if not connection.in_atomic_block:
                close_old_connections()
    return sync_to_async(run)


def _src(scope):
    query = urllib.parse.parse_qs((scope.get("query_string") or b"").decode("latin1"))
    return (query.get("src") or [""])[0]


def _ws_url(base, path, src):
    parts = urllib.parse.urlsplit(base)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return f"{scheme}://{parts.netloc}{path}?src={urllib.parse.quote(src, safe='')}"


def can_watch(scope, src):
    """Whether the websocket's viewer may watch go2rtc stream ``src``.

    With Protect sites, ``src`` must be one of a camera's streams (high, low
    or the echo-cancelled one) and the viewer must see that camera. Without
    them, any stream go2rtc lists (the page shows them all), except talk ones.
    """
    if not src or src.endswith("_talk"):
        return False
    if not config.protect_sites():
        return src in {s["name"] for s in views.get_go2rtc_streams(config.go2rtc_url())}
    for site in views.get_protect_cameras():
        for cam in site.get("cameras", []):
            name = cam.get("stream_name")
            if name and src in (name, f"{name}_low", views.listen_stream_name(cam)):
                return policy.can_view(scope, cam.get("camera_id", ""), views.get_protect_cameras)
    return False


def _targets():
    return config.go2rtc_url(), config.aec_url()


async def watch(scope, receive, send):
    """go2rtc's ``/api/ws`` (WebRTC signalling or MSE) for one allowed stream."""
    await receive()  # websocket.connect
    src = _src(scope)
    if not await _db(can_watch)(scope, src):
        await send({"type": "websocket.close", "code": FORBIDDEN})
        return
    go2rtc, _ = await _db(_targets)()
    await wsrelay.relay(receive, send, _ws_url(go2rtc, "/api/ws", src))


async def talk(scope, receive, send):
    """The kiosk's microphone to a camera's speaker, with a token from ``talk_prepare``."""
    await receive()  # websocket.connect
    stream = redeem_talk(_src(scope))
    if stream is None:
        await send({"type": "websocket.close", "code": FORBIDDEN})
        return
    go2rtc, aec = await _db(_targets)()
    # With echo cancelling, talk goes through aec (it relays to go2rtc).
    await wsrelay.relay(receive, send, _ws_url(aec or go2rtc, "/api/talk", stream))


websocket_urlpatterns = [
    path(WATCH_PATH.lstrip("/"), watch, name="camera_watch_ws"),
    path(TALK_PATH.lstrip("/"), talk, name="camera_talk_ws"),
]
