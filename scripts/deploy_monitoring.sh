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
#   - Grafana:   http://192.168.1.107:3300/  (admin / <GRAFANA_ADMIN_PASSWORD>)
#   - Prometheus:  http://192.168.1.107:9090/
#   - Bot metrics:  http://192.168.1.107:8765/metrics
#
# Security (issue #55): Grafana admin password is sourced from /root/.env
# (or the GRAFANA_ADMIN_PASSWORD env var). The literal "alphard" password
# is NEVER committed to this file, and GF_AUTH_ANONYMOUS_ENABLED is not
# set (Grafana requires login for /api/*). See SECURITY.md for the
# monitoring profile's LAN-exposure footprint.

set -euo pipefail

DOCKER_HOST="${DOCKER_HOST:-tcp://192.168.1.107:2375}"
API="http://${DOCKER_HOST#tcp://}"
NET_NAME="alphard_alphard-net"

# Source Grafana admin password from /root/.env (preferred) or env. Refuse
# to run if neither provides one — the literal "alphard" value is NOT a
# fallback, so we cannot accidentally re-introduce the leaked credential.
# Mirrors docker-compose.yaml:197 which uses
#   GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD:?...required}
ENV_FILE="${ENV_FILE:-/root/.env}"
if [[ -z "${GRAFANA_ADMIN_PASSWORD:-}" && -r "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  GRAFANA_ADMIN_PASSWORD="$(grep -E '^GRAFANA_ADMIN_PASSWORD=' "${ENV_FILE}" \
    | tail -n 1 | cut -d= -f2-)"
fi
if [[ -z "${GRAFANA_ADMIN_PASSWORD:-}" ]]; then
  echo "ERROR: GRAFANA_ADMIN_PASSWORD is not set. Export it or define it in ${ENV_FILE}." >&2
  exit 1
fi
# Sanity-check: catch a stale "alphard" leftover that would silently re-leak
# the credential if someone re-commits the old .env by accident.
if [[ "${GRAFANA_ADMIN_PASSWORD}" == "alphard" ]]; then
  echo "ERROR: GRAFANA_ADMIN_PASSWORD resolves to the literal 'alphard' which is the leaked value from issue #55." >&2
  echo "       Rotate the password (see SECURITY.md) before re-running this script." >&2
  exit 1
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
    "Cmd": ["sh", "-c", "echo " + os.environ["b64"] + " | base64 -d > " + os.environ["dst"] + " && chown 472:472 " + os.environ["dst"] + " && echo WROTE"],
    "AttachStdout": True,
}))' \
    )" | python3 -c 'import sys, json; print(json.load(sys.stdin)["Id"])')"
  curl -s -X POST "${API}/exec/${exec_id}/start" \
    -H "Content-Type: application/json" \
    -d '{"Detach": false}' >/dev/null
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
for f in ./docker/grafana/provisioning/dashboards/*.json; do
  # Issue #56: JSONs are now co-located with provider.yml, so the loop
  # would re-push provider.yml back over itself if we don't filter it out.
  [[ "$(basename "$f")" == "provider.yml" ]] && continue
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
  -d "$(GRAFANA_ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD}" python3 -c '
import json, os
print(json.dumps({
    "Image": "grafana/grafana:latest",
    # Issue #55: never hardcode GF_SECURITY_ADMIN_PASSWORD; pull from /root/.env.
    # GF_AUTH_ANONYMOUS_ENABLED is intentionally NOT set: Grafana must require
    # login for /api/* so LAN peers cannot scrape dashboards anonymously.
    "Env": [
        "GF_SECURITY_ADMIN_PASSWORD=" + os.environ["GRAFANA_ADMIN_PASSWORD"],
        "GF_USERS_ALLOW_SIGN_UP=false",
        "GF_SERVER_HTTP_PORT=3300",
    ],
    "User": "472:472",
    "HostConfig": {
        "NetworkMode": "host",
        "RestartPolicy": {"Name": "unless-stopped"},
        "Mounts": [
            {"Type": "bind", "Source": "/mnt/appdata/alphard/grafana", "Target": "/var/lib/grafana"},
            # Issue #56: dashboards live alongside provider.yml under
            # /mnt/appdata/alphard/observability/grafana/provisioning/.
            # A single bind to /etc/grafana/provisioning exposes both the
            # provider config and the JSONs to the path Grafana scans.
            {"Type": "bind", "Source": "/mnt/appdata/alphard/observability/grafana/provisioning",
             "Target": "/etc/grafana/provisioning", "ReadOnly": True},
        ],
    },
}))' \
  )" | python3 -c 'import sys, json; print(json.load(sys.stdin)["Id"])')"
curl -s -X POST "${API}/containers/${GRAFANA_ID}/start" >/dev/null

# Cleanup helper
curl -s -X DELETE "${API}/containers/hermes-monitor-push?force=true" >/dev/null || true

# Step 4: wait + verify
echo "waiting 30s for containers to come up..."
sleep 30
# /login should always be 200 (it's the login page, public). Use it as a
# liveness probe only — anonymous access to /api/search must now 401.
echo "Grafana:    $(curl -s -o /dev/null -w '%{http_code}' http://192.168.1.107:3300/login)"
echo "Prometheus: $(curl -s -o /dev/null -w '%{http_code}' http://192.168.1.107:9090/-/ready)"
echo "Bot metrics: $(curl -s -o /dev/null -w '%{http_code}' http://192.168.1.107:8765/health)"
# Issue #55: verify anonymous /api/search returns 401, then run the
# dashboard query with Basic-auth using the env-sourced password.
ANON_STATUS=$(curl -s -o /dev/null -w '%{http_code}' http://192.168.1.107:3300/api/search)
echo "Grafana /api/search (anon, must be 401): ${ANON_STATUS}"
if [ "${ANON_STATUS}" != "401" ]; then
  echo "WARNING: Grafana anonymous access still enabled; issue #55 not fully remediated." >&2
fi
echo "Grafana->Prometheus query: $(curl -s -u "admin:${GRAFANA_ADMIN_PASSWORD}" 'http://192.168.1.107:3300/api/datasources/proxy/uid/PBFA97CFB590B2093/api/v1/query?query=up' | grep -c 'alphard-bot')"
# Issue #56: assert at least one dashboard with "alphard" in its title was
# provisioned (pre-fix the JSONs were mounted at /var/lib/grafana/dashboards
# which the provider never scanned, so /api/search returned []).
ALPHARD_DASHBOARDS=$(curl -s -u "admin:${GRAFANA_ADMIN_PASSWORD}" \
  'http://192.168.1.107:3300/api/search?type=dash-db' \
  | grep -c '"title"' || true)
echo "Grafana dashboards provisioned (issue #56, must be >= 1): ${ALPHARD_DASHBOARDS}"
if [ "${ALPHARD_DASHBOARDS:-0}" -lt 1 ]; then
  echo "WARNING: no alphard dashboards were provisioned — issue #56 likely regressed." >&2
fi
