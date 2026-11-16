"""Event replay clips: exported from Protect to disk, then streamed from there.

A 2K camera's 30 s export is tens of MB. Holding it in memory (and a second
copy for the preloaded next chunk) OOM-killed the app's container, which also
restarts go2rtc and drops every live stream. So exports go straight to a file,
at most ``MAX_EXPORTS`` run at once, and the response reads the file in chunks
(with Range support, which ``<video>`` uses for seeking).
"""
import asyncio
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path

from django.http import HttpResponse, StreamingHttpResponse

logger = logging.getLogger(__name__)

CHUNK = 64 * 1024
MAX_EXPORTS = 2
CACHE_BYTES = 512 * 1024 * 1024
CACHE_SECONDS = 3600

_exports = threading.BoundedSemaphore(MAX_EXPORTS)
_stripes = [threading.Lock() for _ in range(16)]
_SAFE = re.compile(r"[^A-Za-z0-9_-]")
_RANGE = re.compile(r"bytes=(\d*)-(\d*)")


def clip_dir():
    return Path(os.environ.get("BDH_DATA_DIR", "/data")) / "clips"


def get(camera_id, start, end, export, timeout):
    """Path of the clip file for this window, or None if Protect had nothing.

    ``export(path)`` writes the MP4 to ``path``. Concurrent requests for the
    same window share one export.
    """
    folder = clip_dir()
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{_SAFE.sub('', camera_id)}-{int(start.timestamp())}-{int(end.timestamp())}.mp4"
    path = folder / name
    with _stripes[hash(name) % len(_stripes)]:
        if _ready(path):
            os.utime(path)
            return path
        if not _exports.acquire(timeout=timeout):
            raise TimeoutError("too many clip exports")
        tmp = folder / f".{uuid.uuid4().hex}.part"
        try:
            export(tmp)
            if not _ready(tmp):
                return None
            os.replace(tmp, path)
        finally:
            _exports.release()
            tmp.unlink(missing_ok=True)
    prune(keep=path)
    return path


def _ready(path):
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def prune(keep=None, now=None):
    """Drop clips older than CACHE_SECONDS, then the oldest until under CACHE_BYTES."""
    now = time.time() if now is None else now
    files = []
    for entry in clip_dir().iterdir():
        try:
            st = entry.stat()
        except OSError:
            continue
        if now - st.st_mtime > CACHE_SECONDS and entry != keep:
            entry.unlink(missing_ok=True)
        elif entry.suffix == ".mp4":
            files.append((st.st_mtime, st.st_size, entry))
    total = sum(size for _, size, _ in files)
    for _, size, entry in sorted(files, key=lambda f: f[0]):
        if total <= CACHE_BYTES:
            break
        if entry != keep:
            entry.unlink(missing_ok=True)
            total -= size


def response(request, path, cache_control):
    """Stream ``path`` (honouring a single ``Range``) without loading it into memory."""
    # Opened now so a prune between here and the first read can't lose it.
    f = open(path, "rb")
    size = os.fstat(f.fileno()).st_size
    first, last, status = 0, size - 1, 200
    m = _RANGE.fullmatch(request.headers.get("Range", "").strip())
    if m and (m[1] or m[2]):
        if m[1]:
            first = int(m[1])
            last = min(int(m[2]), size - 1) if m[2] else size - 1
        else:
            first = max(size - int(m[2]), 0)
        if first >= size or first > last:
            f.close()
            resp = HttpResponse(status=416)
            resp["Content-Range"] = f"bytes */{size}"
            return resp
        status = 206
    length = last - first + 1
    resp = StreamingHttpResponse(_read(f, first, length), content_type="video/mp4", status=status)
    resp["Content-Length"] = str(length)
    resp["Accept-Ranges"] = "bytes"
    resp["Cache-Control"] = cache_control
    if status == 206:
        resp["Content-Range"] = f"bytes {first}-{last}/{size}"
    return resp


async def _read(f, offset, length):
    # Async so Django's ASGI handler streams it (a sync iterator gets list()ed).
    try:
        await asyncio.to_thread(f.seek, offset)
        while length > 0:
            chunk = await asyncio.to_thread(f.read, min(CHUNK, length))
            if not chunk:
                break
            length -= len(chunk)
            yield chunk
    finally:
        f.close()
