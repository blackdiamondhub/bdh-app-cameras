#!/bin/sh
# Run the app wheel in the hub's app runtime image the way the supervisor
# does (code read-only at /app/current, SQLite in /data, 96 MB limit), check
# that it migrates and serves its health check, then report memory.
#   sh scripts/container_smoke.sh dist/bdh_app_cameras-*.whl
# The Protect host is unreachable on purpose: the events worker still imports
# uiprotect and keeps retrying, which is most of what it costs at rest.
set -eu
wheel=${1:?usage: container_smoke.sh WHEEL}
image=${RUNTIME_IMAGE:-ghcr.io/blackdiamondlodge/bdh-app-runtime:hub-api-2}
name=cameras-smoke-$$
work=$(mktemp -d "${PWD}/.smoke.XXXXXX")
trap 'docker rm -f "$name" >/dev/null 2>&1 || true; rm -rf "$work"' EXIT

mkdir -p "$work/current"
python3 -m zipfile -e "$wheel" "$work/current"
chmod -R a+rX "$work"

docker run -d --name "$name" --memory 96m --read-only --tmpfs /tmp \
    --mount type=volume,dst=/data \
    -v "$work/current:/app/current:ro" \
    -e BDH_APP_ID=cameras \
    -e BDH_APP_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')" \
    -e BDH_CAMERAS_PROTECT_HOST_1=192.0.2.1 \
    -e BDH_CAMERAS_PROTECT_API_KEY_1=x \
    -e BDH_CAMERAS_PROTECT_USERNAME_1=smoke \
    -e BDH_CAMERAS_PROTECT_PASSWORD_1=smoke \
    -e BDH_GO2RTC_URL=http://127.0.0.1:1984 \
    -p 127.0.0.1::8000 "$image" >/dev/null
port=$(docker port "$name" 8000/tcp | head -n1 | sed 's/.*://')

ok=
for _ in $(seq 1 60); do
    if curl -sf "http://127.0.0.1:$port/_bdh/health" >/dev/null; then ok=1; break; fi
    sleep 1
done
if [ -z "$ok" ]; then
    docker logs "$name" 2>&1 | tail -n 80
    echo "cameras did not become healthy"; exit 1
fi
curl -sf "http://127.0.0.1:$port/_bdh/health"; echo

# Let the events worker import uiprotect and fail its first connection.
sleep 20
docker logs "$name" 2>&1 | tail -n 20
if docker logs "$name" 2>&1 | grep -E "ImportError|ModuleNotFoundError|OperationalError"; then
    echo "unexpected errors in the container log"; exit 1
fi
test "$(docker inspect -f '{{.State.Running}} {{.State.OOMKilled}}' "$name")" = "true false"
docker exec "$name" python -c "
s = dict(l.split() for l in open('/sys/fs/cgroup/memory.stat'))
cur = int(open('/sys/fs/cgroup/memory.current').read())
ws = (cur - int(s['inactive_file'])) / 2**20
print(f'cameras memory: working_set={ws:.1f}MB anon={int(s[\"anon\"]) / 2**20:.1f}MB limit=96MB')
"
