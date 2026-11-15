"""Resize and cache historical Protect frames.

Protect ignores ``width`` for historical snapshots and returns the full
sensor frame (~1.6 MB at 2688x1512), so we downscale here and keep the
small JPEGs in a byte-capped in-process LRU.
"""
import io
import logging
import threading
import time
from collections import OrderedDict

logger = logging.getLogger(__name__)

CACHE_BYTES = 12 * 1024 * 1024
TS_STEP = 5  # seconds; scrub positions within this window share a frame
# The scrubber's frame card asks for 320 px while dragging and the full view
# for 1280 px on release; a cold fetch at one fills the cache for the other.
WARM_WIDTHS = (320, 1280)

_cache = OrderedDict()
_size = 0
_lock = threading.Lock()


def round_ts(dt):
    return dt.replace(second=dt.second - dt.second % TS_STEP, microsecond=0)


def get(key):
    with _lock:
        data = _cache.get(key)
        if data is not None:
            _cache.move_to_end(key)
        return data


def put(key, data):
    global _size
    if len(data) > CACHE_BYTES:
        return
    with _lock:
        old = _cache.pop(key, None)
        if old is not None:
            _size -= len(old)
        _cache[key] = data
        _size += len(data)
        while _size > CACHE_BYTES:
            _, evicted = _cache.popitem(last=False)
            _size -= len(evicted)


def clear():
    global _size
    with _lock:
        _cache.clear()
        _size = 0
    with _live_lock:
        _live.clear()


def warm(camera_id, ts, data, width):
    """Cache ``data`` at the other WARM_WIDTHS (only when ``width`` is one of them)."""
    if width not in WARM_WIDTHS:
        return
    for w in WARM_WIDTHS:
        key = (camera_id, ts, w)
        if w == width or get(key) is not None:
            continue
        small = resize(data, w)
        if small is not data:
            put(key, small)


def warm_async(camera_id, ts, data, width):
    threading.Thread(target=warm, args=(camera_id, ts, data, width), daemon=True).start()


# Live stills are only card posters (shown until the stream's first frame). A
# Protect fetch takes ~0.5-1 s, which left cards black on first paint, so serve
# the last still at once and refresh it in the background. Posters requested
# recently are kept warm so the next page load is instant too.
LIVE_FRESH = 30          # seconds before a cached still is refreshed
LIVE_MAX_AGE = 15 * 60   # older stills are fetched synchronously (day/night may have changed)
LIVE_WARM_FOR = 2 * 3600  # keep refreshing posters requested within this window
LIVE_WARM_EVERY = 60

_live = {}  # (camera_id, width) -> [fetched_at, data, last_requested]
_live_busy = set()
_live_lock = threading.Lock()
_warmer = None


def live_get(key, fetch, now=None):
    """Return a live still for ``key``; ``fetch()`` gets a new one (may raise)."""
    now = time.monotonic() if now is None else now
    with _live_lock:
        entry = _live.get(key)
        if entry is not None:
            entry[2] = now
            age = now - entry[0]
            if age <= LIVE_MAX_AGE:
                if age > LIVE_FRESH:
                    _refresh_async(key, fetch)
                _ensure_warmer(fetch)
                return entry[1]
    data = fetch(key)
    if data:
        with _live_lock:
            _live[key] = [time.monotonic(), data, now]
            _ensure_warmer(fetch)
    return data


def _refresh(key, fetch):
    try:
        data = fetch(key)
        if data:
            with _live_lock:
                last = _live.get(key, [0, b'', time.monotonic()])[2]
                _live[key] = [time.monotonic(), data, last]
    except Exception as err:
        logger.debug("Live still refresh failed for %s: %s", key, err)
    finally:
        with _live_lock:
            _live_busy.discard(key)


def _refresh_async(key, fetch):
    # Caller holds _live_lock.
    if key in _live_busy:
        return
    _live_busy.add(key)
    threading.Thread(target=_refresh, args=(key, fetch), daemon=True).start()


def _ensure_warmer(fetch):
    # Caller holds _live_lock.
    global _warmer
    if _warmer is None or not _warmer.is_alive():
        _warmer = threading.Thread(target=_warm_loop, args=(fetch,), daemon=True, name='poster-warmer')
        _warmer.start()


def _warm_loop(fetch):
    while True:
        time.sleep(LIVE_WARM_EVERY)
        now = time.monotonic()
        with _live_lock:
            for key, (fetched, _data, last) in list(_live.items()):
                if now - last > LIVE_WARM_FOR:
                    del _live[key]
                elif now - fetched > LIVE_FRESH:
                    _refresh_async(key, fetch)


def resize(data, width):
    """Downscale a JPEG to ``width`` px wide; returns the input if it's already small."""
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(data))
        if img.width <= width:
            return data
        img.draft('RGB', (width, img.height * width // img.width))
        img = img.convert('RGB')
        img.thumbnail((width, img.height * width // img.width), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, 'JPEG', quality=80, optimize=True)
        return out.getvalue()
    except Exception as err:
        logger.warning("Could not resize snapshot; serving original: %s", err)
        return data
