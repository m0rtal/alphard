FROM ghcr.io/m0rtal/alphard:latest

# supercronic — a cron-like scheduler that runs as PID 1 in a container.
# Static Go binary, no shell, no user service. Perfect for a one-shot
# cron container. We use it instead of vixie cron because cron requires
# a crond daemon + PAM config; supercronic just exec's the command.
ARG SUPERCRONIC_VERSION=v1.2.4
ARG SUPERCRONIC_SHA=71b0d58cc53f6bd72cf2f043e09c1b60e285cc38923eb0a41c1a25c33b007f9c6
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSL "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64" \
    -o /usr/local/bin/supercronic \
 && chmod +x /usr/local/bin/supercronic \
 && rm -rf /var/lib/apt/lists/*

# alphard-bot image already has src/ + requirements + t-tech-investments.
# The crontab expects `python /app/scripts/daily_sync.py` etc.
COPY docker/cron/crontab.txt /etc/crontab
RUN mkdir -p /app/logs && chown -R alphard:alphard /app/logs

USER alphard
WORKDIR /app

# Cron runs jobs as user alphard (unprivileged). Commands must use
# python from PATH (already configured by the base image).
CMD ["supercronic", "/etc/crontab"]
