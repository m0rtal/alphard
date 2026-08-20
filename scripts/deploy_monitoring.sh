#!/usr/bin/env bash
# scripts/deploy_monitoring.sh — first-shot deploy of Prometheus + Grafana on .107.
#
# Why this script exists:
# The .107 Docker daemon has broken bridge port mapping (iptables NAT empty after
# ContainerStart). Anything that depends on standard host:<port> -> container:<port>
# binding will fail to be reachable from the host. Workaround: deploy Grafana
# with --network host, which makes it share the host network namespace and
# means 0.0.0.0:3300 inside the container = 0.0.0.0:3300 on the host.
#
# The Prometheus datasource URL inside Grafana is then http://localhost:9090
# (Grafana's localhost is the host's localhost because of host network mode),
# and Prometheus IS reachable on host:9090 because Prometheus is in the regular
# bridge network with port mapping 9090:9090 (which DOES work standalone).
#
# Pre-reqs:
#   - alphard-bot is running on host (binds 0.0.0.0:8765/metrics)
#   - /root/.env exists on Hermes host (not used here, but the alphard-bot
#     container reads it via env_file in docker-compose.yaml)
#   - Docker API is reachable on .107:2375 (this script reads DOCKER_HOST env)
#   - /mnt/appdata/alphard/ exists on .107 (created by alphard stack)
#
# Idempotent: re-running is safe; existing containers are removed first.
#
# Usage:
#   DOCKER_HOST=tcp://192.168.1.107:2375 ./scripts/deploy_monitoring.sh
#
# After deploy:
#   - Grafana:   http://192.168.1.107:3300/  (admin / $GRAFANA_ADMIN_PASSWORD)
#   - Prometheus:  http://192.168.1.107:9090/
#   - Bot metrics:  http://192.168.1.107:8765/metrics
#
# SECURITY (issue #55): anonymous auth is OFF and the admin password comes
# from $GRAFANA_ADMIN_PASSWORD (sourced from $ALPHARD_ENV_FILE or ./env).
# Refuses to run if the password is missing or set to the historical
# literal "alphard".

set -euo pipefail

DOCKER_HOST="${DOCKER_HOST:-tcp://192.168.1.107:2375}"
API="http://${DOCKER_HOST#tcp://}"
NET_NAME="alphard_alphard-net"

# Load Grafana admin password from the .env file. The deploy script runs on
# the Hermes host where the project root contains a populated .env (see
# README.md step 2: cp .env.example .env && set GRAFANA_ADMIN_PASSWORD).
# Override via $ALPHARD_ENV_FILE if .env lives elsewhere. We refuse to run
# without a non-empty GRAFANA_ADMIN_PASSWORD so we never accidentally
# deploy with the historical "alphard" literal that was committed in the
# first version of this script (issue #55).
ALPHARD_ENV_FILE="${ALPHARD_ENV_FILE:-./.env}"
if [[ -f "$ALPHARD_ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a
  . "$ALPHARD_ENV_FILE"
  set +a
fi
if [[ -z "${GRAFANA_ADMIN_PASSWORD:-}" ]]; then
  echo "ERROR: GRAFANA_ADMIN_PASSWORD is not set." >&2
  echo "  Set it in $ALPHARD_ENV_FILE (copy from .env.example) or" >&2
  echo "  export GRAFANA_ADMIN_PASSWORD=... before re-running." >&2
  exit 2
fi
# Refuse the historical literal so a forgotten .env.example copy can't
# silently deploy with a known-public password.
if [[ "$GRAFANA_ADMIN_PASSWORD" == "alphard" ]]; then
  echo "ERROR: GRAFANA_ADMIN_PASSWORD is set to the historical literal 'alphard'" >&2
  echo "  (issue #55). Generate a new password with:" >&2
  echo "    openssl rand -base64 24" >&2
  exit 2
fi

# Step 1: copy provisioning files from local repo to .107 bind-target dir.
# /root/projects/alphard/ does not exist on .107, so we cannot bind-mount
# directly. We stage files into /mnt/appdata/alphard/observability/ which IS
# on .107.
APP_DATA_DIR="/mnt/appdata/alphard"
OBS_DIR="${APP_DATA_DIR}/observability"
PROM_DIR="${OBS_DIR}/prometheus"
GRAF_PROV_DIR="${OBS_DIR}/grafana/provisioning"
GRAF_DASH_DIR="${GRAF_PROV_DIR}/dashboards"

mkdir -p "${PROM_DIR}" "${GRAF_DASH_DIR}" "${GRAF_PROV_DIR}/datasources"

# Push provisioning files via a temporary helper container.
HELPER_ID="$(curl -s -X POST "${API}/containers/create?name=hermes-monitor-push" \
  -H "Content-Type: application/json" \
  -d '{
    "Image": "alpine:3.20",
    "Cmd": ["sleep", "600"],
    "HostConfig": {
      "Mounts": [{"Type": "bind", "Source": "/mnt/appdata", "Target": "/mnt/appdata", "ReadOnly": false}],
      "AutoRemove": false
    }
  }' | python3 -c 'import sys, json; print(json.load(sys.stdin)["Id"])')"
curl -s -X POST "${API}/containers/${HELPER_ID}/start" >/dev/null

push_file() {
  local src="$1" dst="$2"
  # Stream the file via base64 + `echo ... | base64 -d > ...` inside
  # the helper container. We build the JSON body with python (already
  # used elsewhere in this script for JSON parsing) so b64 chars
  # (+/=) and the dst path are escaped safely. Pre-fix (issue #54)
  # this inlined `$b64` inside an already-shell-escaped JSON string,
  # which broke at parse time — the script had a syntax error on
  # line 65 (missing closing `"`) AND never actually invoked the
  # function from anywhere.
  local b64
  b64="$(base64 -w0 < "$src")"
  local exec_id
  exec_id="$(curl -s -X POST "${API}/containers/${HELPER_ID}/exec" \
    -H "Content-Type: application/json" \
    -d "$(b64="$b64" dst="$dst" python3 -c '
import json, os
print(json.dumps({
    "Cmd": ["sh", "-c", "echo " + os.environ["b64"] + " | base64 -d > " + os.environ["dst"] + " && chown 472:472 " + os.environ["dst"] + " && echo WROTE || { echo FAIL >&2; exit 1; }"],
    "AttachStdout": True,
    "AttachStderr": True,
}))' \
    )" | python3 -c 'import sys, json; print(json.load(sys.stdin)["Id"])')"
  # Issue #71: capture the exec_start response and assert the
  # in-container sh produced the WROTE marker. Pre-fix the response
  # was discarded (> /dev/null) so any failure inside the helper
  # container (base64 decode error, write permission denied, missing
  # parent dir) was invisible to the operator — provisioning silently
  # failed and Prometheus / Grafana started with stale or missing
  # config files.
  #
  # The Docker Engine API returns the exec response as a
  # stdcopy-formatted stream when Detach: false (8-byte header per
  # frame: type + size, then the payload). The `WROTE` marker is
  # ASCII and survives the framing intact. We strip the 8-byte
  # stdcopy headers via `dd bs=1 skip=8` per chunk, but a simpler
  # heuristic that handles real output correctly is to grep for the
  # marker on the raw stream: Docker's stdcopy header starts with
  # byte 0x01 (stdout) or 0x02 (stderr), so the substring "WROTE"
  # cannot collide with the framing bytes.
  local exec_output
  exec_output="$(curl -s -X POST "${API}/exec/${exec_id}/start" \
    -H "Content-Type: application/json" \
    -d '{"Detach": false}')"
  if ! grep -q WROTE <<<"$exec_output"; then
    echo "ERROR: push_file $src -> $dst failed" >&2
    echo "Helper container response (raw):" >&2
    echo "$exec_output" >&2
    return 1
  fi
  # Post-write integrity check: re-read the pushed file via the
  # helper container and compare its sha256sum to the local source.
  # Catches cases where the in-container `echo WROTE` fired but the
  # actual write went to the wrong path (tyo, symlink race, etc).
  local local_sha remote_sha
  local_sha="$(sha256sum < "$src" | awk '{print $1}')"
  remote_sha="$(curl -s -X POST "${API}/containers/${HELPER_ID}/exec" \
    -H "Content-Type: application/json" \
    -d "$(dst="$dst" python3 -c '
import json, os
print(json.dumps({
    "Cmd": ["sh", "-c", "sha256sum \"" + os.environ["dst"] + "\" 2>/dev/null || echo NOSUCH"],
    "AttachStdout": True,
}))')" \
    | python3 -c 'import sys, json; print(json.load(sys.stdin)["Id"])')"
  local remote_output
  remote_output="$(curl -s -X POST "${API}/exec/${remote_sha}/start" \
    -H "Content-Type: application/json" \
    -d '{"Detach": false}')"
  # Extract the first 64 hex chars from the response. The helper
  # outputs "<sha>  <path>\n" so a simple grep -oE pulls it out.
  remote_sha="$(grep -aoE '[0-9a-f]{64}' <<<"$remote_output" | head -n 1)"
  if [[ -z "$remote_sha" ]]; then
    echo "ERROR: push_file $src -> $dst: post-write sha256 read returned no hash (file missing or unreadable in helper)" >&2
    return 1
  fi
  if [[ "$remote_sha" != "$local_sha" ]]; then
    echo "ERROR: push_file $src -> $dst: sha256 mismatch after write" >&2
    echo "  local:  $local_sha" >&2
    echo "  remote: $remote_sha" >&2
    return 1
  fi
}

# Step 1.5: actually push the provisioning files onto the .107 host.
# Pre-fix (PR #53) the `push_file` function was defined but never
# called, so the script silently failed to provision anything on a
# clean host. See issue #54 — `bash -n scripts/deploy_monitoring.sh`
# returned parse error AND the only path that copies files was dead.
push_file "./docker/prometheus/prometheus.yml" \
  "/mnt/appdata/alphard/observability/prometheus/prometheus.yml"
push_file "./docker/grafana/provisioning/datasources/prometheus.yml" \
  "/mnt/appdata/alphard/observability/grafana/provisioning/datasources/prometheus.yml"
push_file "./docker/grafana/provisioning/dashboards/provider.yml" \
  "/mnt/appdata/alphard/observability/grafana/provisioning/dashboards/provider.yml"
# Dashboards: provision one JSON at a time so partial failures don't
# block the whole batch.
for f in ./docker/grafana/dashboards/*.json; do
  push_file "$f" "/mnt/appdata/alphard/observability/grafana/provisioning/dashboards/$(basename "$f")"
done

# Step 2: deploy Prometheus (bridge network, port mapping works standalone).
echo "deploying prometheus..."
curl -s -X DELETE "${API}/containers/alphard-prometheus?force=true" >/dev/null || true
sleep 2
PROM_ID="$(curl -s -X POST "${API}/containers/create?name=alphard-prometheus" \
  -H "Content-Type: application/json" \
  -d '{
    "Image": "prom/prometheus:latest",
    "Cmd": [
      "--config.file=/etc/prometheus/prometheus.yml",
      "--storage.tsdb.path=/prometheus",
      "--storage.tsdb.retention.time=30d"
    ],
    "HostConfig": {
      "RestartPolicy": {"Name": "unless-stopped"},
      "Mounts": [
        {"Type": "bind", "Source": "/mnt/appdata/alphard/observability/prometheus/prometheus.yml",
         "Target": "/etc/prometheus/prometheus.yml", "ReadOnly": true},
        {"Type": "bind", "Source": "/mnt/appdata/alphard/prometheus", "Target": "/prometheus"}
      ],
      "NetworkMode": "alphard_alphard-net",
      "PortBindings": {"9090/tcp": [{"HostIp": "", "HostPort": "9090"}]}
    }
  }' | python3 -c 'import sys, json; print(json.load(sys.stdin)["Id"])')"
curl -s -X POST "${API}/containers/${PROM_ID}/start" >/dev/null

# Step 3: deploy Grafana (host network — see file header for why).
echo "deploying grafana..."
curl -s -X DELETE "${API}/containers/alphard-grafana?force=true" >/dev/null || true
sleep 2
GRAFANA_ID="$(curl -s -X POST "${API}/containers/create?name=alphard-grafana" \
  -H "Content-Type: application/json" \
  -d "$(GRAFANA_ADMIN_PASSWORD="$GRAFANA_ADMIN_PASSWORD" python3 -c '
import json, os
print(json.dumps({
    "Image": "grafana/grafana:latest",
    "Env": [
        "GF_SECURITY_ADMIN_PASSWORD=" + os.environ["GRAFANA_ADMIN_PASSWORD"],
        # Anonymous auth disabled (issue #55). The historical literal was a
        # one-shot bootstrap convenience that exposed every provisioned
        # dashboard to any TCP client on the LAN. Authentication is now required.
        "GF_USERS_ALLOW_SIGN_UP=false",
        "GF_SERVER_HTTP_PORT=3300",
    ],
    "User": "472:472",
    "HostConfig": {
        "NetworkMode": "host",
        "RestartPolicy": {"Name": "unless-stopped"},
        "Mounts": [
            {"Type": "bind", "Source": "/mnt/appdata/alphard/grafana", "Target": "/var/lib/grafana"},
            {"Type": "bind", "Source": "/mnt/appdata/alphard/observability/grafana/provisioning",
             "Target": "/etc/grafana/provisioning", "ReadOnly": True},
            {"Type": "bind", "Source": "/mnt/appdata/alphard/observability/grafana/provisioning/dashboards",
             "Target": "/var/lib/grafana/dashboards", "ReadOnly": True}
        ]
    }
}))' \
  )" | python3 -c 'import sys, json; print(json.load(sys.stdin)["Id"])')"
curl -s -X POST "${API}/containers/${GRAFANA_ID}/start" >/dev/null

# Cleanup helper
curl -s -X DELETE "${API}/containers/hermes-monitor-push?force=true" >/dev/null || true

# Step 4: wait + verify
echo "waiting 30s for containers to come up..."
sleep 30
echo "Grafana:    $(curl -s -o /dev/null -w '%{http_code}' http://192.168.1.107:3300/login)"
echo "Prometheus: $(curl -s -o /dev/null -w '%{http_code}' http://192.168.1.107:9090/-/ready)"
echo "Bot metrics: $(curl -s -o /dev/null -w '%{http_code}' http://192.168.1.107:8765/health)"
echo "Grafana->Prometheus query: $(curl -s -u "admin:${GRAFANA_ADMIN_PASSWORD}" 'http://192.168.1.107:3300/api/datasources/proxy/uid/PBFA97CFB590B2093/api/v1/query?query=up' | grep -c 'alphard-bot')"
echo "Grafana /api/search anonymous (expect 401): $(curl -s -o /dev/null -w '%{http_code}' 'http://192.168.1.107:3300/api/search')"
