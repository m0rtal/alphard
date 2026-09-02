# Deployment: env-file sourcing for alphard-bot

This document explains how the alphard-bot container receives its
environment (notably the long Tinkoff tokens that exceed Portainer's
60-char Env-parameter limit) and the operational guardrails around it.

## TL;DR

| Piece | Where | Purpose |
|-------|-------|---------|
| Long tokens | Host `/root/.env` (real **file**) | The only place a 64-char Tinkoff token can live |
| ENV_FILE override | Portainer Env-parameter on stack #97, value `ENV_FILE=/root/.env` | Tells `docker/entrypoint.sh` which file to source |
| entrypoint logic | `docker/entrypoint.sh`, candidate list lines 14-21 | Sources the first existing file from the candidate list |
| Bind-mounted candidates | compose `env_file:` directive + `/run/secrets/alphard_env` | Legacy fallback — see "Docker 29.x bind-mount quirk" below |

## The problem (issue #84)

alphard-bot crashloops with:

```
Neither TINKOFF_SANDBOX_TOKEN nor TINKOFF_REAL_TOKEN is set.
```

This message is unambiguous but the *root cause* is not obvious: it
fires whenever the entrypoint's env-file sourcing step produces
nothing, regardless of *why* nothing was sourced. The two known
causes on .107 are:

1. **Docker 29.1.x bind-mount quirk**: when you bind-mount
   `src=/root/.env dst=/run/secrets/alphard_env` and the source is a
   *directory* (or the leaf path doesn't pre-exist in the container),
   Docker creates an empty **directory** at the leaf instead of failing
   the mount. The entrypoint then does `[ -f /run/secrets/alphard_env ]`
   → false → loop continues. No token, no broker, exit 1, restart loop.

2. **No ENV_FILE override wired**: even after fixing the bind-mount
   issue, if compose does not pass an explicit `ENV_FILE=…` env var
   into the container, the entrypoint never receives the host's real
   `/root/.env` path. It only tries the bind-mounted candidates.

The fix is two halves:

* **PR #27** (already merged) added the `ENV_FILE` candidate to
  `docker/entrypoint.sh` — the entrypoint now reads from
  `${ENV_FILE}` first if set.
* **This PR (#84)** wires `ENV_FILE: /root/.env` through Portainer
  Env-parameter on stack #97 so the override actually reaches the
  entrypoint.

## The deployment contract

1. **The host `/root/.env` MUST be a real file.** Not a directory.
   If it became a directory (manual `mkdir`, accidental `cp` of a
   directory, etc.), the bind-mount quirk above will silently break
   token sourcing. Verify with:
   ```sh
   ssh root@192.168.1.107 'test -f /root/.env && echo FILE || echo DIRECTORY'
   ```
   If you see DIRECTORY, fix it with:
   ```sh
   ssh root@192.168.1.107 'rm -rf /root/.env && touch /root/.env && chmod 600 /root/.env'
   ```
   then re-populate from your password manager.

2. **Portainer Env-parameter on stack #97 includes
   `ENV_FILE=/root/.env`.** This is a single short string (15 chars),
   well under the 60-char Portainer limit. Do NOT put Tinkoff tokens
   themselves in Portainer Env — they are 64+ chars and will be
   truncated by Go's JSON unmarshal limit.

3. **Long tokens live ONLY in the file body.** The compose file's
   `TINKOFF_SANDBOX_TOKEN: ${TINKOFF_SANDBOX_TOKEN:?…}` line expands
   the variable from the local `.env` file **at compose-render time
   on the Portainer host**. Portainer itself reads `.env` from
   `/data/compose/97/.env` (not `/root/.env`). Make sure both files
   are in sync — see "Syncing .env files" below.

## Docker 29.x bind-mount quirk — what to avoid

**Don't**:

* bind-mount `/root/.env` (or any file path) into `/run/secrets/…`
  when the leaf path doesn't pre-exist in the container image. The
  29.1.x daemon will silently create a directory there and your
  mount will look successful but contain nothing.
* Use `env_file: .env` in compose and rely on Portainer resolving it
  from `/data/compose/97/.env`. That works, but it is **a different
  file** from the host's `/root/.env`. If you only update one of
  them you will get drift.

**Do**:

* Use the `ENV_FILE=/root/.env` env var pattern (this PR). The
  entrypoint sources whatever path you point at, so it works for any
  file location — `/root/.env`, `/etc/alphard/env`, a Docker secret
  mounted as `/run/secrets/alphard.env`, etc.
* Always verify after a Portainer stack update:
  ```sh
  ssh root@192.168.1.107 'docker exec alphard-bot printenv | grep -E "ENV_FILE|TINKOFF"'
  ```
  `ENV_FILE=/root/.env` must be present and `TINKOFF_SANDBOX_TOKEN`
  must start with the expected prefix.

## Syncing .env files on .107

There are two `.env` files in play:

| Path | Read by | Notes |
|------|---------|-------|
| `/root/.env` | alphard-bot container, via `ENV_FILE=/root/.env` | Sourced by entrypoint.sh at runtime |
| `/data/compose/97/.env` | Portainer stack renderer, at compose-render time | Used for `${VAR:?...}` expansion in compose YAML |

If you update tokens, update **both** files. The simplest way is to
symlink, but Portainer does not follow symlinks for `.env` files —
you have to copy:

```sh
ssh root@192.168.1.107 'install -m 0600 /root/.env /data/compose/97/.env'
```

Or, if you're regenerating tokens, write both files in the same
script.

## Verification

After a redeploy:

1. **Container is running, not restarting:**
   ```sh
   ssh root@192.168.1.107 'docker ps --filter name=alphard-bot --format "{{.Status}}"'
   ```
   Should show `Up X minutes (healthy)` not `Restarting (1)`.

2. **Tokens are loaded inside the container:**
   ```sh
   ssh root@192.168.1.107 'docker exec alphard-bot printenv TINKOFF_SANDBOX_TOKEN | head -c 12'
   ```
   Should print `t.XXXX…` (sandbox token prefix) without erroring
   out — i.e. the variable must be **set** to a real value, not empty.

3. **Metrics endpoint is reachable:**
   ```sh
   curl -sk http://192.168.1.107:8765/metrics | grep alphard_heartbeats_total
   ```
   Should print a non-empty value.

4. **`alphard-web` reachable at `:8081` and renders the dashboard.**
   Open <http://192.168.1.107:8081/> in a browser; the auth-prompt
   should accept your `ALPHARD_WEB_TOKEN` (PR #394, gated by
   PR #406 / #411). _(Grafana heartbeat panel removed, PR #399 —
   the replacement observability surface is `alphard-web`.)_

## What entrypoint.sh does (source-level reference)

```sh
for ENV_FILE_CANDIDATE in \
    "${ENV_FILE:-}" \
    "/run/secrets/alphard.env" \
    "/run/secrets/alphard_env" \
    "/tmp/alphard.env"; do
    if [ -n "${ENV_FILE_CANDIDATE}" ] && [ -f "${ENV_FILE_CANDIDATE}" ]; then
        set -a
        . "${ENV_FILE_CANDIDATE}"
        set +a
        break
    fi
done
```

* The first candidate is `${ENV_FILE}` — the explicit override.
* The other candidates are the legacy bind-mount paths and the
  manual-`docker cp` fallback for the Docker 29.x quirk.
* `set -a` auto-exports every variable that the sourced file sets.
  This is the POSIX-portable equivalent of `source` in bash — it
  works because `docker/entrypoint.sh` runs under Alpine's BusyBox
  `ash`, which does not have `declare -x` or bash-only `source`.
* `[ -n "${ENV_FILE_CANDIDATE}" ]` skips the empty-ENV_FILE case so
  we don't try to source an empty-string path (which would fail
  with a confusing `ENV_FILE_CANDIDATE: No such file` error).

If you change the candidate list, update
`tests/test_entrypoint_env_file.py::_SCAN_SNIPPET` and
`test_candidate_order_matches_entrypoint` in lockstep.

## Troubleshooting

| Symptom | Likely cause | First check |
|---------|--------------|-------------|
| Crashloop, "Neither TINKOFF_*_TOKEN is set" | ENV_FILE unset or path wrong | `docker exec alphard-bot printenv ENV_FILE` |
| Container starts but auth_probe fails | Token rotated, postgres volume has stale scram hash | See `scripts/check_db_password.py` and the entrypoint.sh DSN-fingerprint comment block |
| Tokens set in container but `/metrics` 404 | Bot process never started, only entrypoint probe ran | `docker logs alphard-bot --tail 100` |
| `/metrics` returns 200 but `alphard_heartbeats_total` stays 0 | Heartbeat thread wedged; backfill supervisor is the only active loop | `docker exec alphard-bot ps auxf` — should show `python -m src.main` and one backfill child |
