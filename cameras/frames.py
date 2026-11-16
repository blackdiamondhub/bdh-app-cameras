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


def render(camera_id, ts, data, width):
    """Return the full-size frame ``data`` at ``width`` and cache it.

    A warm width also caches the other WARM_WIDTHS from the same decode, so a
    scrub costs one decode per frame, not one per width.
    """
    widths = WARM_WIDTHS if width in WARM_WIDTHS else (width,)
    out = resize_many(data, widths)
    for w, small in out.items():
        if w == width or small is not data:
            put((camera_id, ts, w), small)
    return out[width]


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


def live_peek(key, now=None):
    """The cached live still for ``key`` (never fetches), or None."""
    now = time.monotonic() if now is None else now
    with _live_lock:
        entry = _live.get(key)
        if entry is None or now - entry[0] > LIVE_MAX_AGE:
            return None
        entry[2] = now
        return entry[1]


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


# Each decode holds a few MB of pixels; a fast scrub fires many requests at
# once, and unbounded decodes pushed the 128 MiB container into the OOM killer.
DECODE_SLOTS = 2
_decode_slots = threading.BoundedSemaphore(DECODE_SLOTS)
_decoding = 0
_decoding_lock = threading.Lock()

# Pillow allocates pixels in big blocks. After the first big free glibc raises
# its mmap threshold, so later blocks come from the heap and stay there,
# fragmented: the app's baseline crept from ~68 to ~106 MiB over a soak, which
# left a scrub's transient decodes no room under the limit. A fixed threshold
# keeps big blocks mmapped (returned on free); a trim after each burst of
# decodes hands back what the heap kept anyway.
M_MMAP_THRESHOLD = -3
MMAP_THRESHOLD = 128 * 1024


def _load_libc():
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6")
        libc.mallopt(M_MMAP_THRESHOLD, MMAP_THRESHOLD)
        return libc
    except (OSError, AttributeError):
        return None  # not glibc (tests on Windows/macOS)


_libc = _load_libc()


def _trim():
    if _libc is not None:
        _libc.malloc_trim(0)


def resize(data, width):
    """Downscale a JPEG to ``width`` px wide; returns the input if it's already small."""
    return resize_many(data, (width,))[width]


def resize_many(data, widths):
    """Downscale a JPEG to each of ``widths`` from one decode -> {width: bytes}.

    Widths the image is already within map to the input unchanged.
    """
    global _decoding
    widths = sorted(set(widths), reverse=True)
    result = {}
    with _decode_slots:
        with _decoding_lock:
            _decoding += 1
        try:
            _decode_into(data, widths, result)
        except Exception as err:
            logger.warning("Could not resize snapshot; serving original: %s", err)
        finally:
            with _decoding_lock:
                _decoding -= 1
                idle = _decoding == 0
        # The pixels are freed once _decode_into returns; trim when the burst
        # ends rather than after every frame of a scrub.
        if idle:
            _trim()
    for w in widths:
        result.setdefault(w, data)
    return result


def _decode_into(data, widths, result):
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    smaller = [w for w in widths if img.width > w]
    if smaller:
        first = smaller[0]
        img.draft('RGB', (first, img.height * first // img.width))
        img = img.convert('RGB')
        # Largest first: each thumbnail shrinks ``img`` in place, so the
        # smaller widths are cut from the previous result.
        for w in smaller:
            img.thumbnail((w, img.height * w // img.width), Image.LANCZOS)
            out = io.BytesIO()
            img.save(out, 'JPEG', quality=80, optimize=True)
            result[w] = out.getvalue()
