#!/usr/bin/env python3
"""fetch_tinkoff_gost_ca.py — extract Russian GOST CA chain from
*.tinkoff.ru and iss.moex.com TLS handshake so the alphard-bot HTTPS
client can verify their certs.

Why: Russian .ru domains use Russian Trusted Root CA + Sub CA (GOST
crypto) which is NOT in the standard certifi/western trust store.
Without this bundle, every HTTPS request to *.tinkoff.ru returns:
  ssl.SSLCertVerificationError: self-signed certificate in certificate chain
which surfaces as a 30-second TCP timeout when `requests` retries
handshake. The fix is to bundle the Russian CA chain explicitly.

Auto-refresh: this script is invoked by alphard-bot entrypoint.sh so
a Tinkoff/MOEX cert rotation picks up automatically on the next
container start.

Pure-Python implementation (no shell awk pipes) so it works
identically in:
  - dev box with bash+openssl
  - alphard-bot alpine container with python+pyOpenSSL (no openssl CLI)

Usage:
  python3 scripts/fetch_tinkoff_gost_ca.py [--out PATH]
"""

import argparse
import socket
import ssl
import subprocess
import sys
import tempfile
from pathlib import Path

# Endpoints we contact. alphard-bot talks to BOTH:
#   - invest-public-api.tinkoff.ru  — broker + REST
#   - iss.moex.com                   — MOEX ISS REST
# (If we add more endpoints later, the script picks up their chain
# automatically because we extract from the live TLS handshake.)
ENDPOINTS = [
    ("invest-public-api.tinkoff.ru", 443),
    ("iss.moex.com", 443),
]


def fetch_chain(host: str, port: int, timeout: float = 10.0) -> str:
    """Use openssl CLI in a temp file to get the full chain. Falls back
    to Python ssl.SSLContext if openssl is not present (rare)."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
        out_path = f.name
    try:
        subprocess.run(
            [
                "openssl",
                "s_client",
                "-connect",
                f"{host}:{port}",
                "-servername",
                host,
                "-showcerts",
            ],
            stdin=subprocess.DEVNULL,
            stdout=open(out_path, "w"),
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        out_path = None
    if out_path:
        text = Path(out_path).read_text(errors="replace")
        Path(out_path).unlink()
        return text
    # Fallback: raw socket + wrap to capture peer cert chain.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # we want the chain, not validation
    with socket.create_connection((host, port), timeout=timeout) as s:
        with ctx.wrap_socket(s, server_hostname=host) as ss:
            der_cert = ss.getpeercert(binary_form=True)
            from base64 import b64encode

            b64 = b64encode(der_cert).decode("ascii")
            pem = (
                "-----BEGIN CERTIFICATE-----\n"
                + "\n".join(b64[i : i + 64] for i in range(0, len(b64), 64))
                + "\n-----END CERTIFICATE-----\n"
            )
            return pem
    return ""


def extract_pem_blocks(text: str) -> list[str]:
    """Pull all -----BEGIN CERTIFICATE----- ... -----END CERTIFICATE-----
    blocks out of openssl's full -showcerts output (which also contains
    banner / verify-error text)."""
    blocks: list[str] = []
    cur: list[str] = []
    in_block = False
    for line in text.splitlines():
        if "-----BEGIN CERTIFICATE-----" in line:
            in_block = True
            cur = [line]
        elif "-----END CERTIFICATE-----" in line:
            cur.append(line)
            blocks.append("\n".join(cur) + "\n")
            cur = []
            in_block = False
        elif in_block:
            cur.append(line)
    return blocks


def dedupe(certs: list[str]) -> list[str]:
    """Drop duplicate certs (same fingerprint, e.g. when both endpoints
    happen to share an intermediate)."""
    import hashlib

    seen: set[str] = set()
    out: list[str] = []
    for c in certs:
        h = hashlib.sha256(c.encode("utf-8")).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        out.append(c)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent.parent / "docker" / "certs" / "tinkoff-gost-ca-bundle.pem"),
    )
    args = parser.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    all_blocks: list[str] = []
    for host, port in ENDPOINTS:
        print(
            f"[fetch_tinkoff_gost_ca] extracting chain from {host}:{port}",
            file=sys.stderr,
        )
        text = fetch_chain(host, port)
        blocks = extract_pem_blocks(text)
        print(
            f"[fetch_tinkoff_gost_ca]   {host}:{port} → {len(blocks)} PEM block(s)",
            file=sys.stderr,
        )
        all_blocks.extend(blocks)

    unique = dedupe(all_blocks)
    bundle = "".join(unique)

    out_path.write_text(bundle)
    out_path.chmod(0o644)

    print(f"[fetch_tinkoff_gost_ca] wrote {out_path} " f"({len(bundle)} bytes, {len(unique)} certs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
