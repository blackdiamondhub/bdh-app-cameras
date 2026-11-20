"""UniFi Protect API client for dynamic camera discovery.

Supports multiple Protect sites configured in cameras.config.protect_sites().
Each site has: host, api_key, name.

Uses the UniFi Protect Integration (Public) API with API key auth.
- GET  /proxy/protect/integration/v1/cameras           — list cameras
- POST /proxy/protect/integration/v1/cameras/{id}/rtsps-stream — create RTSPS URL
- GET  /proxy/protect/integration/v1/cameras/{id}/rtsps-stream — get existing URL
Auth: X-API-KEY header
"""

import ipaddress
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import urllib3

from django.core.cache import cache as django_cache

from . import config

logger = logging.getLogger(__name__)

# Suppress InsecureRequestWarning for self-signed NVR certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CACHE_TTL = 300  # 5 minutes
# Shorter TTL when a refresh was incomplete (e.g. Protect rate-limited us), so
# missing cameras / PTZ controls come back quickly instead of after 5 minutes.
INCOMPLETE_CACHE_TTL = 30
CACHE_KEY_PREFIX = 'protect_cameras_'

API_BASE = '/proxy/protect/integration/v1'

# Protect's integration API returns 429 when hit with a burst of requests.
MAX_PARALLEL_REQUESTS = 4
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BACKOFF_S = 1.0


def _request(method, url, **kwargs):
    """requests.<method> with retry/backoff on HTTP 429 (Too Many Requests)."""
    func = getattr(requests, method)
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        resp = func(url, **kwargs)
        if resp.status_code != 429 or attempt == RATE_LIMIT_RETRIES:
            return resp
        try:
            delay = float(resp.headers.get('Retry-After'))
        except (TypeError, ValueError):
            delay = RATE_LIMIT_BACKOFF_S * (2 ** attempt)
        logger.info("Protect rate-limited %s; retrying in %.1fs", url, delay)
        time.sleep(delay)
    return resp


def _camera_name_to_stream_name(name):
    """Convert a camera display name to a safe go2rtc stream name.

    E.g. "Front Door" -> "front_door", "Back-Yard Cam" -> "back_yard_cam"
    """
    return name.lower().replace(' ', '_').replace('-', '_')


def get_protect_cameras():
    """Fetch cameras from all configured UniFi Protect sites, with caching.

    Returns a list of site dicts:
        [{'id': '1', 'name': 'Sun Peaks', 'host': ..., 'cameras': [...], 'ok': True}, ...]

    ``ok`` is False when the site couldn't be reached. Each camera dict has:
    name, camera_id, stream_name, rtsp_url, is_ptz, ptz_presets, is_doorbell
    and detections (``{'supported': [...], 'enabled': [...]}``, our kinds).
    Returns an empty list if no sites are configured.
    """
    sites = config.protect_sites()
    if not sites:
        return []

    # Separate cached vs uncached sites
    result_map = {}  # index -> site dict
    to_fetch = []    # (index, site) pairs needing API calls

    for i, site in enumerate(sites):
        host = site['host']
        cache_key = f'{CACHE_KEY_PREFIX}{host}'
        cached = django_cache.get(cache_key)
        if cached is not None:
            logger.debug("Camera cache HIT for %s", host)
            result_map[i] = {
                'id': site.get('id'),
                'name': site.get('name', host),
                'host': host,
                'cameras': cached,
                'ok': True,
            }
        else:
            logger.info("Camera cache MISS for %s", host)
            to_fetch.append((i, site))

    # Fetch uncached sites in parallel
    if to_fetch:
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=len(to_fetch)) as pool:
            futures = {}
            for i, site in to_fetch:
                host = site['host']
                api_key = site['api_key']
                site_name = site.get('name', host)
                f = pool.submit(
                    _fetch_cameras_from_site_ex, host, api_key, site_name,
                )
                futures[f] = (i, site)

            for f in as_completed(futures):
                i, site = futures[f]
                host = site['host']
                site_name = site.get('name', host)
                cameras, complete = f.result()
                ok = cameras is not None
                if ok:
                    cache_key = f'{CACHE_KEY_PREFIX}{host}'
                    ttl = CACHE_TTL if complete else INCOMPLETE_CACHE_TTL
                    django_cache.set(cache_key, cameras, ttl)
                    if complete and site.get('id'):
                        try:
                            config.remember_detections(site['id'], cameras)
                        except Exception:
                            logger.exception("Could not store the detections for %s", host)
                else:
                    cameras = []
                result_map[i] = {
                    'id': site.get('id'),
                    'name': site_name,
                    'host': host,
                    'cameras': cameras,
                    'ok': ok,
                }
        logger.info("Fetched cameras from %d site(s) in %.1fs",
                    len(to_fetch), time.time() - t0)

    return [result_map[i] for i in sorted(result_map)]


def clear_cache():
    """Clear the camera cache for all sites."""
    sites = config.protect_sites()
    for site in sites:
        django_cache.delete(f'{CACHE_KEY_PREFIX}{site["host"]}')
        django_cache.delete(f'{PTZ_CACHE_KEY_PREFIX}{site["host"]}')
    logger.info("Camera cache cleared for %d sites", len(sites))


def _get_rtsps_url(host, api_key, camera_id):
    """Get or create RTSPS stream URLs for a camera (high and low quality).

    First tries GET to retrieve existing streams. If none exist,
    creates them via POST with both 'high' and 'low' qualities.

    Returns a dict {'high': url, 'low': url} with available URLs,
    or None on failure. At minimum 'high' will be present.
    """
    base = f'https://{host}{API_BASE}/cameras/{camera_id}/rtsps-stream'
    headers = {'X-API-KEY': api_key}

    # Try to get existing RTSPS streams
    try:
        resp = _request('get', base, headers=headers, verify=False, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        urls = {}
        for quality in ('high', 'low'):
            if data.get(quality):
                urls[quality] = _clean_rtsps_url(data[quality])
        if 'high' in urls and 'low' in urls:
            return urls
        # Have high but missing low — fall through to POST to create both
    except requests.RequestException:
        pass  # Fall through to create

    # No existing streams — create both high and low
    try:
        resp = _request(
            'post',
            base,
            headers={**headers, 'Content-Type': 'application/json'},
            json={'qualities': ['high', 'low']},
            verify=False,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        urls = {}
        for quality in ('high', 'low'):
            if data.get(quality):
                urls[quality] = _clean_rtsps_url(data[quality])
        if urls:
            return urls
    except requests.RequestException as e:
        logger.warning("Failed to create RTSPS stream for camera %s: %s",
                       camera_id, e)

    return None


def _clean_rtsps_url(url):
    """Strip ?enableSrtp from RTSPS URLs — go2rtc handles TLS natively."""
    return url.split('?')[0] if url else url


def ptz_goto_preset(camera_id, slot):
    """Move a PTZ camera to the given preset slot.

    Looks up the correct site by searching cached camera data.
    Returns True on success (204), False on failure.
    """
    # Find which site owns this camera
    host, api_key = _find_site_for_camera(camera_id)
    if not host:
        logger.warning("Camera %s not found in any configured site", camera_id)
        return False

    url = f'https://{host}{API_BASE}/cameras/{camera_id}/ptz/goto/{slot}'
    headers = {'X-API-KEY': api_key}

    try:
        resp = requests.post(url, headers=headers, verify=False, timeout=10)
        if resp.status_code == 204:
            logger.info("PTZ camera %s moved to preset %d", camera_id, slot)
            return True
        else:
            logger.warning("PTZ goto failed for camera %s slot %d: %d",
                           camera_id, slot, resp.status_code)
            return False
    except requests.RequestException as e:
        logger.error("PTZ goto request failed for camera %s: %s", camera_id, e)
        return False


def _find_site_for_camera(camera_id):
    """Find the site (host, api_key) that owns a camera_id.

    Searches cached camera data first. Returns (host, api_key) or (None, None).
    """
    sites = config.protect_sites()

    # Search cache for the camera
    for site in sites:
        host = site['host']
        cached = django_cache.get(f'{CACHE_KEY_PREFIX}{host}')
        if cached:
            for cam in cached:
                if cam['camera_id'] == camera_id:
                    return host, site['api_key']

    # Not in cache — try all sites
    for site in sites:
        return site['host'], site['api_key']

    return None, None


PTZ_DEFAULT_PRESETS = 4
PTZ_CACHE_KEY_PREFIX = 'protect_ptz_'
PTZ_CACHE_TTL = 24 * 3600


def _is_ptz_camera(host, api_key, camera_id, camera_type=''):
    """Detect if a camera supports PTZ without moving it.

    UniFi PTZ models carry "PTZ" in their type (e.g. "UVC G5 PTZ"), which
    needs no extra request. Otherwise probes POST /ptz/goto/1000 (a slot
    that won't exist):
    - 404 = PTZ camera (preset not found, but camera supports PTZ)
    - 400 = Not a PTZ camera ("Camera is not a type of PTZ")

    Returns True/False, or None if the probe couldn't get an answer.
    """
    if 'ptz' in (camera_type or '').lower():
        return True

    # A camera's PTZ support never changes, so remember the probe answer and
    # don't re-probe every camera on each 5-minute refresh (that burst is
    # what trips Protect's rate limit). clear_cache() forgets these too.
    cache_key = f'{PTZ_CACHE_KEY_PREFIX}{host}'
    known = (django_cache.get(cache_key) or {}).get(camera_id)
    if known is not None:
        return known

    url = f'https://{host}{API_BASE}/cameras/{camera_id}/ptz/goto/1000'
    headers = {'X-API-KEY': api_key}

    try:
        resp = _request('post', url, headers=headers, verify=False, timeout=10)
    except requests.RequestException:
        return None
    if resp.status_code in (404, 400):
        is_ptz = resp.status_code == 404
        # Probes run in parallel, so a racing write may drop an entry; that
        # camera is simply probed again next refresh.
        known = django_cache.get(cache_key) or {}
        known[camera_id] = is_ptz
        django_cache.set(cache_key, known, PTZ_CACHE_TTL)
        return is_ptz
    logger.warning("PTZ probe for camera %s returned %d", camera_id,
                   resp.status_code)
    return None


TALKBACK_CACHE_KEY_PREFIX = 'protect_talkback_'
TALKBACK_CACHE_TTL = 24 * 3600
TALKBACK_RATES = (8000, 12000, 16000, 24000, 48000)
_TALKBACK_URL = re.compile(r'^rtp://(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})$')


def _parse_talkback(data):
    """Protect's talkback-session reply -> {'url', 'codec', 'rate'}, or None if unusable.

    Only Opus to a private IPv4 is usable; go2rtc's talkback source refuses anything else.
    """
    if not isinstance(data, dict) or data.get('codec') != 'opus':
        return None
    m = _TALKBACK_URL.match(str(data.get('url') or ''))
    rate = data.get('samplingRate')
    if not m or rate not in TALKBACK_RATES:
        return None
    try:
        ip = ipaddress.IPv4Address(m.group(1))
    except ValueError:
        return None
    port = int(m.group(2))
    if not ip.is_private or not 0 < port < 65536:
        return None
    return {'url': f'rtp://{ip}:{port}', 'codec': 'opus', 'rate': rate}


def _get_talkback(host, api_key, camera_id, refresh=False):
    """The camera's talkback target ({'url', 'codec', 'rate'}), or None.

    Protect hands back the same target on every POST, so it is cached for a
    day; ``refresh`` asks again. Failures are logged and never cached.
    """
    cache_key = f'{TALKBACK_CACHE_KEY_PREFIX}{camera_id}'
    if not refresh:
        cached = django_cache.get(cache_key)
        if cached:
            return cached
    try:
        resp = _request(
            'post',
            f'https://{host}{API_BASE}/cameras/{camera_id}/talkback-session',
            headers={'X-API-KEY': api_key},
            verify=False,
            timeout=10,
        )
        resp.raise_for_status()
        talkback = _parse_talkback(resp.json())
    except (requests.RequestException, ValueError) as e:
        logger.warning("Talkback session for camera %s failed: %s", camera_id, e)
        return None
    if talkback is None:
        logger.warning("Talkback session for camera %s is unusable", camera_id)
        return None
    django_cache.set(cache_key, talkback, TALKBACK_CACHE_TTL)
    return talkback


def talk_stream_name(stream_name):
    return f'{stream_name}_talk'


def talkback_source(talkback):
    """go2rtc source for a talkback target (see go2rtc-nack patch 0005)."""
    return f"talkback:{talkback['url']}?codec=opus&rate={talkback['rate']}"


def refresh_talkback(camera_id):
    """Ask Protect for the camera's talkback target again and update the camera cache.

    Returns the cached camera dict (with fresh ``talkback``), or None if the
    camera isn't known or can't take talkback.
    """
    for site in config.protect_sites():
        cache_key = f"{CACHE_KEY_PREFIX}{site['host']}"
        cameras = django_cache.get(cache_key) or []
        cam = next((c for c in cameras if c.get('camera_id') == camera_id), None)
        if cam is None:
            continue
        if not cam.get('has_speaker'):
            return None
        talkback = _get_talkback(site['host'], site['api_key'], camera_id, refresh=True)
        if talkback is None:
            return None
        if talkback != cam.get('talkback'):
            cam = dict(cam, talkback=talkback)
            django_cache.set(cache_key, [cam if c.get('camera_id') == camera_id else c
                                         for c in cameras], CACHE_TTL)
        return cam
    return None


def _detections(camera, is_doorbell):
    """What a camera can detect (``supported``) and has turned on in Protect (``enabled``), as our kinds.

    A doorbell always rings and every camera sees motion.
    """
    flags = camera.get('featureFlags') or {}
    smart = camera.get('smartDetectSettings') or {}
    base = (['doorbell'] if is_doorbell else []) + ['motion']
    supported = config.protect_kinds(
        list(flags.get('smartDetectTypes') or []) + list(flags.get('smartDetectAudioTypes') or []))
    enabled = config.protect_kinds(
        list(smart.get('objectTypes') or []) + list(smart.get('audioTypes') or []))
    order = config.EVENT_KINDS
    return {
        'supported': [k for k in order if k in base or k in supported],
        'enabled': [k for k in order if k in base or k in enabled],
    }


def _fetch_cameras_from_site(host, api_key, site_name=None):
    """Like _fetch_cameras_from_site_ex but returns only the camera list."""
    return _fetch_cameras_from_site_ex(host, api_key, site_name)[0]


def _fetch_cameras_from_site_ex(host, api_key, site_name=None):
    """Fetch all cameras with RTSPS streams from a single Protect site.

    Uses API key authentication (X-API-KEY header). Discovers cameras via
    GET /v1/cameras, then fetches/creates RTSPS stream URLs and probes
    PTZ support for all cameras in a single parallel pass.

    When site_name is provided, stream names are prefixed with the site
    name to avoid collisions when multiple sites have cameras with the
    same name (e.g., both sites have "Front Door").

    PTZ capability comes from the camera type when it says "PTZ", otherwise
    from probing a non-existent preset slot (1000), which returns 404 for PTZ
    cameras and 400 for non-PTZ cameras — without moving the camera.

    Returns (cameras, complete): cameras is a list of camera dicts (None on
    failure); complete is False if any per-camera request failed (e.g. Protect
    rate limiting), so callers can avoid caching a partial result for long.
    """
    if not all([host, api_key]):
        logger.warning("UniFi Protect not configured (need host and API key)")
        return None, False

    try:
        resp = _request(
            'get',
            f'https://{host}{API_BASE}/cameras',
            headers={'X-API-KEY': api_key},
            verify=False,
            timeout=15,
        )
        resp.raise_for_status()
        cameras_data = resp.json()

        # Handle both list and dict formats
        if isinstance(cameras_data, dict):
            cameras_data = list(cameras_data.values())

        # Build basic camera info (no RTSPS yet)
        pending = []
        detections = {}
        audio = {}
        for camera in cameras_data:
            name = camera.get('name', 'Unknown')
            camera_id = camera.get('id')
            if not camera_id:
                continue
            if site_name:
                stream_name = _camera_name_to_stream_name(
                    f"{site_name} {name}",
                )
            else:
                stream_name = _camera_name_to_stream_name(name)
            pending.append((camera_id, name, stream_name, camera.get('type', '')))
            detections[camera_id] = _detections(
                camera, 'doorbell' in (camera.get('type') or '').lower())
            flags = camera.get('featureFlags') or {}
            audio[camera_id] = {
                'has_mic': bool(flags.get('hasMic')) and camera.get('isMicEnabled') is not False,
                'has_speaker': bool(flags.get('hasSpeaker')),
            }

        # Fetch RTSPS URLs and probe PTZ in a single parallel pass
        cameras = []
        complete = True
        if not pending:
            return cameras, complete
        workers = min(len(pending) * 2, MAX_PARALLEL_REQUESTS)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            rtsps_futures = {
                pool.submit(_get_rtsps_url, host, api_key, cid): (cid, name, sn)
                for cid, name, sn, _ in pending
            }
            ptz_futures = {
                pool.submit(_is_ptz_camera, host, api_key, cid, ctype): cid
                for cid, name, sn, ctype in pending
            }
            talk_futures = {
                pool.submit(_get_talkback, host, api_key, cid): cid
                for cid, _, _, _ in pending if audio[cid]['has_speaker']
            }

            # Collect RTSPS results
            types = {cid: ctype or '' for cid, _, _, ctype in pending}
            rtsps_results = {}
            for f in as_completed(rtsps_futures):
                camera_id, name, stream_name = rtsps_futures[f]
                rtsps_urls = f.result()
                if rtsps_urls:
                    rtsps_results[camera_id] = {
                        'name': name,
                        'camera_id': camera_id,
                        'stream_name': stream_name,
                        'rtsp_url': rtsps_urls.get('high', ''),
                        'rtsp_url_low': rtsps_urls.get('low', ''),
                        'is_ptz': False,
                        'ptz_presets': 0,
                        'is_doorbell': 'doorbell' in types.get(camera_id, '').lower(),
                        'detections': detections[camera_id],
                        **audio[camera_id],
                        'talkback': None,
                    }
                else:
                    complete = False

            # Collect PTZ results
            for f in as_completed(ptz_futures):
                camera_id = ptz_futures[f]
                is_ptz = f.result()
                if is_ptz is None:
                    complete = False
                if camera_id in rtsps_results and is_ptz:
                    rtsps_results[camera_id]['is_ptz'] = True
                    rtsps_results[camera_id]['ptz_presets'] = PTZ_DEFAULT_PRESETS

            # A camera without talkback is still a camera; don't shorten the cache for it.
            for f in as_completed(talk_futures):
                camera_id = talk_futures[f]
                if camera_id in rtsps_results:
                    rtsps_results[camera_id]['talkback'] = f.result()

            cameras = list(rtsps_results.values())

        cameras.sort(key=lambda c: c['name'])
        return cameras, complete
    except requests.RequestException as e:
        logger.error("Failed to fetch cameras from UniFi Protect: %s", e)
        return None, False
