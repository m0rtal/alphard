#!/bin/sh
# Alphard — Grafana entrypoint wrapper (compose refactor 2.0, kanban t_884fec4a)
#
# Decodes Grafana provisioning + dashboard base64 blobs from ENV into
# /etc/grafana/provisioning and /var/lib/grafana/dashboards, then execs
# the upstream grafana entrypoint (/run.sh).
#
# Why this script exists
# ----------------------
# The legacy compose bind-mounted ./docker/grafana/{provisioning,dashboards}
# into the container. On .107 PVE LXC + Docker 29.1.x, bind-mount leafs
# are owned by userns-mapped nobody (uid 65534); Grafana (running as
# in-image `grafana` / uid 472) cannot read its own config and restart-
# loops. PR #217 moved the source to ${APPDATA_DIR:-/srv/alphard}/grafana
# so an operator with the right host path could chown 472:472 — but this
# still depends on the host filesystem layout and is fragile across
# operators.
#
# This script removes that dependency entirely: provisioning + dashboard
# files are shipped inline as base64 env vars (baked by tools/bake_grafana_env.py
# and stored in the .env alongside the rest of the stack secrets). On
# container start we decode into tmpfs-backed paths that the container
# owns root:root — writable regardless of userns-mapping, and reproducible
# across hosts.
#
# Why ENV-baked base64 (and not dockerfile COPY):
# ------------------------------------------------
# 1. The grafana/grafana image is upstream. A custom Dockerfile would
#    fork from `grafana/grafana` and ADD ./docker/grafana/... into the
#    image. That works, but it forces every dashboard tweak to ship as
#    a container image rebuild — slow feedback loop (image push + pull
#    through Watchtower) and contradictory to the in-process-config
#    ethos of the rest of the alphard stack (PROM_YML_B64 for prometheus).
# 2. Tools/bake_grafana_env.py runs in CI before compose deploy, so the
#    .env is the single source of truth for *what* dashboards exist; the
#    image is generic. Operators edit the .env (or trigger the bake
#    script against a checked-in dashboard file) and the next StackUpdate
#    on .107 reflects the change.
# 3. ENV size: the largest dashboard is alphard-phase28.json ≈ 14 KiB.
#    base64 inflates by ~33% to ~19 KiB. Plus 3 provisioning YAML files
#    (≤2 KiB total). Total payload ≈ 22 KiB — well under compose's per-
#    service env size limits (Docker daemon accepts ~1 MiB per service
#    env block) and Portainer's stack Env has no hard cap on .env body.
#
# Idempotency
# -----------
# Each `_B64` env var is decoded into a deterministic target path on every
# container start. The previous run's content is overwritten — this is
# intentional and matches the Prometheus PR #147 PROM_YML_B64 pattern.
# Watchtower recreate + alphard-grafana restart both end up with the
# exact same provisioning state derived from the .env.
#
# Failure modes (fail-fast)
# -------------------------
# - Any *_B64 env var that is unset OR not valid base64 → exit 1 with a
#   clear log line naming the offending variable. Loud failure is the
#   design: silent acceptance of half-loaded provisioning surfaces days
#   later as "dashboard disappeared" without an obvious cause.
#
# Why GF_PATHS_PROVISIONING is NOT overridden
# -------------------------------------------
# We write to the upstream default /etc/grafana/provisioning and
# /var/lib/grafana/dashboards. Grafana picks up provisioning paths via
# GF_PATHS_PROVISIONING (default /etc/grafana/provisioning — verified
# on docs.grafana.com/latest/setup-grafana/configure-docker/, "Default
# paths" table). Overriding GF_PATHS_PROVISIONING would be belt-and-
# braces, but the upstream default already points where we write, so
# we skip the extra env var. The compose file does NOT set
# GF_PATHS_PROVISIONING; this entrypoint writes to the default.

set -eu

# Log to stdout so docker logs / kubectl logs / Portainer UI all see
# the same line. No secrets go through here — only the variable names.
log() {
    printf '[alphard-grafana-entrypoint] %s\n' "$*"
}

# Required: each *_B64 var must be present and decode cleanly. We do
# NOT accept an empty default (no `${X_B64:-}` here) — the bake script
# generates real base64 for every variable, so an empty value is a bug
# upstream and we want to surface it loudly on container start.
decode_b64() {
    _var_name="$1"
    _target_path="$2"
    # Indirect expansion: ${!_var_name} does POSIX-portable lookup.
    _b64_value=$(eval "printf '%s' \"\${${_var_name}:-}\"")
    if [ -z "${_b64_value}" ]; then
        log "FATAL: ${_var_name} is unset or empty — re-run tools/bake_grafana_env.py to regenerate .env"
        exit 1
    fi
    # Ensure parent dir exists; -p ignores already-exists, mkdir on
    # /etc/grafana/provisioning/datasources creates both the leaf and
    # any missing intermediates.
    mkdir -p "$(dirname "${_target_path}")"
    # Decode. We use `base64 -d` (GNU coreutils / busybox both ship it).
    # No newline stripping needed: the bake script always emits a single
    # base64 blob with NO trailing newline (textwrap disabled, width=0),
    # so base64 -d emits exactly the original bytes.
    printf '%s' "${_b64_value}" | base64 -d > "${_target_path}" || {
        log "FATAL: ${_var_name} is not valid base64 — re-run tools/bake_grafana_env.py"
        exit 1
    }
    log "wrote ${_target_path} from ${_var_name} ($(wc -c < "${_target_path}") bytes)"
}

log "starting grafana entrypoint (compose refactor 2.0)"

# Provisioning files (datasources + dashboards provider). Both write
# into /etc/grafana/provisioning (the upstream default; verified via
# docs.grafana.com). Grafana scans this dir on startup and (re)loads
# each subdir (datasources/, dashboards/, notifiers/, plugins/, alerting/).
decode_b64 "PROVISIONING_DATASOURCES_YML_B64" "/etc/grafana/provisioning/datasources/prometheus.yml"
decode_b64 "PROVISIONING_DASHBOARDS_PROVIDER_YML_B64" "/etc/grafana/provisioning/dashboards/provider.yml"

# Dashboards. The dashboards provider above points at
# /var/lib/grafana/dashboards — we write each JSON file there.
decode_b64 "DASHBOARD_PHASE0_JSON_B64" "/var/lib/grafana/dashboards/alphard-phase0.json"
decode_b64 "DASHBOARD_PHASE28_JSON_B64" "/var/lib/grafana/dashboards/alphard-phase28.json"

# Grafana runs as uid 472. The decoded files were written by us (root),
# so grafana can read them — but the /var/lib/grafana/dashboards dir
# itself was already present (Grafana's entrypoint created it on first
# boot). The compose file ALSO mounts a named volume at /var/lib/grafana
# (the sqlite + plugins home); that mount hides /var/lib/grafana/dashboards
# unless we created it before the mount took effect. Solution: write
# the dashboards BEFORE the upstream entrypoint runs, and ALSO chown
# the dashboards dir to 472 since root:root would otherwise break
# Grafana's later writes (it doesn't write into dashboards/ after
# startup, but it does chmod-on-write, which silently fails on a
# foreign-owner file).
chown -R 472:472 /var/lib/grafana/dashboards 2>/dev/null || log "chown dashboards failed (LXC bind-mount) — continuing"
chown -R 472:472 /etc/grafana/provisioning 2>/dev/null || log "chown provisioning failed (LXC bind-mount) — continuing"

log "decoded 4 files; exec /run.sh (upstream grafana entrypoint)"
# `exec` so PID 1 is /run.sh — signals (SIGTERM from docker stop) reach
# grafana-server directly. Without exec the shell stays PID 1 and the
# container hangs on shutdown for the full --stop-timeout (10s default).
exec /run.sh
