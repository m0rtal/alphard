"""alphard-web healthcheck — exits 0 if /api/health is reachable, 1 otherwise.

Used by docker-compose healthcheck. Avoids shell-quoting gymnastics by
not nesting quotes inside a YAML string.
"""
from __future__ import annotations

import sys
import urllib.request

URL = "http://127.0.0.1:8080/api/health"


def main() -> int:
    try:
        r = urllib.request.urlopen(URL, timeout=2)
        return 0 if r.status == 200 else 1
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
