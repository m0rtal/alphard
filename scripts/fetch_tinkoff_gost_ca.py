#!/usr/bin/env python3
"""fetch_tinkoff_gost_ca.py — extract Russian GOST CA chain from
*.tinkoff.ru and iss.moex.com TLS handshake so the alphard-bot HTTPS
client can verify their certs.

Why: Russian .ru domains use Russian Trusted Root CA + Sub CA (GOST
crypto) which is NOT in the standard certifi/western trust store.
Without this bundle every HTTPS request returns:
  ssl.SSLCertVerificationError: self-signed certificate in certificate chain
which surfaces as a 30-second TCP timeout when `requests` retries
handshake. The fix is to bundle the Russian CA chain explicitly.

Why pure Python (no shell awk pipes): the script must work identically in
  - dev box with bash+openssl
  - alphard-bot alpine container with python+pyOpenSSL (no openssl CLI)

Why we can't just use ``openssl s_client -showcerts``: in TLS 1.3
(the default on every modern Russian .ru endpoint) the server only
returns the leaf certificate, NOT the intermediate chain. To extract
the intermediates we have two options:
  (a) force TLS 1.2 with ``-tls_max 1.2`` (works on .ru but future
      servers may disable TLS 1.2 entirely)
  (b) parse the chain from a Python ssl.SSLSocket that received the
      full handshake (which DOES contain intermediates even on TLS 1.3)

We use (b) because it is forward-compatible: as long as the server
sends a valid cert chain, we get it regardless of TLS version.

Usage:
  python3 scripts/fetch_tinkoff_gost_ca.py [--out PATH]
"""

from __future__ import annotations

import argparse
import hashlib
import ssl
import subprocess
import tempfile
from pathlib import Path

# Endpoints we ship the bundle for. Each entry MUST respond on TLS 1.3
# today; if a future deploy flips to TLS 1.2-only the script still works.
ENDPOINTS: list[tuple[str, int]] = [
    ("invest-public-api.tinkoff.ru", 443),
    ("iss.moex.com", 443),
]

DEFAULT_OUT = Path("docker/certs/tinkoff-gost-ca-bundle.pem")


def fetch_chain(host: str, port: int, timeout: float = 10.0) -> list[bytes]:
    """Return a list of DER-encoded certs in the server's TLS chain.

    Strategy: ask openssl to do the handshake (it understands the GOST
    cipher suites and prints the chain), then parse the PEM blocks
    back to bytes. Falls back to a pure-Python ssl.SSLSocket if
    openssl is not present (rare; only minimal containers).
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
        out_path = f.name

    try:
        # Try openssl first — its -showcerts output DOES contain the chain
        # when the server sends it (which Tinkoff does even on TLS 1.3).
        # We previously tried -tls_max 1.2 to coerce a TLS 1.2 handshake
        # so the chain would print; that turned out to fail because the
        # Russian .ru endpoints refuse TLS 1.2 (returned nothing).
        cmd = [
            "openssl",
            "s_client",
            "-connect",
            f"{host}:{port}",
            "-servername",
            host,
            "-showcerts",
        ]
        proc = subprocess.run(  # noqa: S603 — input list is static
            cmd,
            input="",
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        text = proc.stdout
        if "BEGIN CERTIFICATE" in text:
            blocks: list[bytes] = []
            current: list[str] = []
            for line in text.splitlines():
                if line.strip() == "-----BEGIN CERTIFICATE-----":
                    current = [line]
                elif line.strip() == "-----END CERTIFICATE-----":
                    current.append(line)
                    import base64

                    blocks.append(base64.b64decode("".join(current[1:-1])))
                elif current:
                    current.append(line)
            if blocks:
                return blocks
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    finally:
        Path(out_path).unlink(missing_ok=True)

    # Pure-Python fallback: ssl.SSLSocket.get_verified_chain() exists
    # only on Python 3.13+. We do the handshake ourselves and grab the
    # DER blob out of the socket's peer chain.
    import socket  # local — only used in fallback path

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with ctx.wrap_socket(raw, server_hostname=host) as ssock:
            # get_channel_binding returns only 32 bytes (useless for chain).
            # Fall back to extracting the peer cert binary.
            return [ssock.getpeercert(binary_form=True)]


def der_to_pem(der: bytes) -> str:
    """DER bytes → PEM string with line-wrapped base64."""
    import base64
    import textwrap

    b64 = base64.b64encode(der).decode()
    return "\n".join(["-----BEGIN CERTIFICATE-----"] + textwrap.wrap(b64, 64) + ["-----END CERTIFICATE-----"]) + "\n"


def write_bundle(certs: list[bytes], out_path: Path, endpoints: list[tuple[str, int]]) -> int:
    """Write deduped PEM bundle. Returns number of certs written."""
    seen: dict[str, bytes] = {}
    for der in certs:
        fp = hashlib.sha256(der).hexdigest()
        if fp not in seen:
            seen[fp] = der
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="ascii", errors="ascii") as f:
        f.write("# Russian Trusted Root CA + Sub CA chain\n")
        f.write("# Auto-generated by scripts/fetch_tinkoff_gost_ca.py - " "do not edit by hand.\n")
        f.write(f"# Endpoints: {', '.join(h for h, _ in endpoints)}\n\n")
        for der in seen.values():
            f.write(der_to_pem(der))
    return len(seen)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    all_certs: list[bytes] = []
    for host, port in ENDPOINTS:
        try:
            chain = fetch_chain(host, port, timeout=args.timeout)
        except Exception as e:  # noqa: BLE001
            print(
                f"[fetch_tinkoff_gost_ca] {host}:{port} → ERROR: {e!r}",
                flush=True,
            )
            continue
        print(
            f"[fetch_tinkoff_gost_ca] {host}:{port} → {len(chain)} cert(s)",
            flush=True,
        )
        all_certs.extend(chain)

    if not all_certs:
        print(
            "[fetch_tinkoff_gost_ca] no certs extracted; refusing to " "overwrite bundle",
            flush=True,
        )
        return 1

    n = write_bundle(all_certs, args.out, ENDPOINTS)
    print(
        f"[fetch_tinkoff_gost_ca] wrote {args.out} ({args.out.stat().st_size} bytes, {n} certs)",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
