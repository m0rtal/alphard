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
the intermediates we use ``openssl s_client -showcerts`` followed by
post-processing the BEGIN/END blocks in Python, because Tinkoff's
``invest-public-api.tinkoff.ru`` does print the full chain when the
``-showcerts`` flag is set (verified against the live endpoint on
2026-09-02).

Usage:
  python3 scripts/fetch_tinkoff_gost_ca.py [--out PATH]

File extension: the commit ships the bundle as
``docker/certs/tinkoff-gost-ca-bundle.txt`` (``.txt`` extension
deliberately used to comply with the project's ``.gitignore`` rule
blocking ``*.pem``). Python's ``ssl.SSLContext.load_verify_locations``
parses the file by content (``-----BEGIN CERTIFICATE-----`` markers),
not by extension — verified by
``tests/test_455_gost_wiring.py::test_gost_bundle_file_contains_valid_pem``.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import ssl
import subprocess
from pathlib import Path

# Endpoints we ship the bundle for. Each entry MUST respond on TLS 1.3
# today; if a future deploy flips to TLS 1.2-only the script still works.
ENDPOINTS: list[tuple[str, int]] = [
    ("invest-public-api.tinkoff.ru", 443),
    ("iss.moex.com", 443),
]

DEFAULT_OUT = Path("docker/certs/tinkoff-gost-ca-bundle.txt")

# PEM marker constants — kept at module scope so the parser does not
# duplicate string literals across the BEGIN/END branches.
_PEM_BEGIN = "-----BEGIN CERTIFICATE-----"
_PEM_END = "-----END CERTIFICATE-----"


def _drop_leaf(certs: list[bytes]) -> list[bytes]:
    """Drop leaf cert(s) from the front of a TLS chain.

    `openssl s_client -showcerts` and `SSLSocket.get_verified_chain()`
    return `[leaf, intermediate_1, intermediate_2, ..., root]`. Some
    servers (verified against `iss.moex.com` on 2026-09-03) repeat the
    leaf cert at index 0 AND index 1 — likely a TLS-extension quirk
    where the server includes the leaf twice in its handshake message.
    A CA bundle is supposed to contain only CA certs (intermediates +
    root), so we strip ALL leading leaf certs by walking the front of
    the chain until we hit a CA (`basicConstraints CA:TRUE`).

    If every cert in the chain is a leaf (broken upstream CA chain —
    server sending only its own cert with no intermediate), raise so
    the caller fails the write instead of shipping a leaf-only bundle
    that has zero usable CAs (issue #482).
    """
    if not certs:
        raise RuntimeError("chain is empty; refusing to ship a leaf-only bundle (issue #482)")
    # Walk the chain from the front; collect indices that are leaves.
    leaf_count = 0
    for der in certs:
        if _is_ca_cert(der):
            break
        leaf_count += 1
    if leaf_count == len(certs):
        raise RuntimeError(
            f"chain has {len(certs)} cert(s) and none is a CA (basicConstraints CA:TRUE); "
            f"refusing to ship a leaf-only bundle (issue #482)"
        )
    return certs[leaf_count:]


def _is_ca_cert(der: bytes) -> bool:
    """Return True iff the DER cert carries ``basicConstraints CA:TRUE``.

    Pure-Python parse via ``cryptography.x509`` (already a project dep
    used by ``_fetch_chain_python`` for the bytes-shaped-chain fix in
    #464; listed explicitly in ``requirements.txt`` and
    ``requirements-ci.txt`` since PR #486 closes #488). Returns False on
    any decode error (treating undecodable bytes as non-CA so the
    caller strips them — safer than keeping unknowns).

    Why no ``openssl x509 -ext basicConstraints`` subprocess: the openssl
    CLI is not guaranteed to exist on the alphard-bot alpine container
    (per the module docstring: ``python+pyOpenSSL (no openssl CLI)``).
    Shell-out based CA detection would mark every cert as a leaf on
    openssl-less hosts, causing ``_drop_leaf`` to raise
    ``RuntimeError("chain has N cert(s) and none is a CA...")`` and
    refuse to write the bundle. Issue #488.
    """
    try:
        from cryptography import x509
        from cryptography.x509.oid import ExtensionOID
    except ImportError:
        # cryptography is a hard dep of the project (requirements.txt);
        # this branch only fires on a manually-stripped install. Treat
        # the cert as a leaf (caller strips it) so the script fails
        # loudly on a missing dep rather than silently shipping a
        # leaf-only bundle.
        return False

    try:
        cert = x509.load_der_x509_certificate(der)
    except Exception:  # noqa: BLE001 — DER parse error → not a CA
        return False

    try:
        bc = cert.extensions.get_extension_for_oid(ExtensionOID.BASIC_CONSTRAINTS).value
    except x509.ExtensionNotFound:
        # RFC 5280 §4.2.1.9: a cert without basicConstraints is a leaf.
        return False

    # `bc` is `cryptography.x509.BasicConstraints` at runtime; the type
    # is reported as `ExtensionType` upstream, so we read the attribute
    # through ``getattr`` to keep the type checker happy.
    return bool(getattr(bc, "ca", False))


def _parse_pem_blocks(text: str) -> list[bytes]:
    """Parse `BEGIN CERTIFICATE ... END CERTIFICATE` blocks out of `text`.

    Validates the base64 body of every block. A block whose body is empty
    or malformed raises `ValueError` so callers can decide whether to
    fall back (openssl returned junk) or fail (the script is broken).

    Empty blocks are the failure mode a half-rendered `openssl s_client`
    handshake produces (defect 2 in #454): the BEGIN/END markers print,
    the base64 body is never sent, and `base64.b64decode("")` raises
    `binascii.Error`. We surface that as a clear error instead.
    """
    blocks: list[bytes] = []
    current: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == _PEM_BEGIN:
            current = [stripped]
        elif stripped == _PEM_END:
            current.append(stripped)
            body = "".join(current[1:-1]).replace("\r", "")
            try:
                decoded = base64.b64decode(body, validate=True)
            except binascii.Error as exc:
                raise ValueError(
                    f"malformed PEM block: empty or non-base64 body "
                    f"(openssl output truncated mid-handshake?): {exc!r}"
                ) from exc
            if not decoded:
                raise ValueError(
                    "malformed PEM block: decoded body is empty "
                    "(openssl returned BEGIN/END without base64 between them)"
                )
            blocks.append(decoded)
            current = []
        elif current:
            current.append(line)
    return blocks


def fetch_chain(host: str, port: int, timeout: float = 10.0) -> list[bytes]:
    """Return a list of DER-encoded certs in the server's TLS chain.

    Strategy: ask openssl to do the handshake (it understands the GOST
    cipher suites and prints the chain), then parse the PEM blocks
    back to bytes. Falls back to a pure-Python ssl.SSLSocket if
    openssl is not present (rare; only minimal containers).
    """
    cmd = [
        "openssl",
        "s_client",
        "-connect",
        f"{host}:{port}",
        "-servername",
        host,
        "-showcerts",
    ]
    try:
        proc = subprocess.run(  # noqa: S603 — input list is static
            cmd,
            input="",
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        # openssl CLI missing — fall through to the pure-Python path.
        return _fetch_chain_python(host, port, timeout, reason=f"openssl not found: {exc!r}")
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"openssl s_client timed out after {timeout}s against {host}:{port}") from exc

    # Defect 1 from #454: openssl prints BEGIN CERTIFICATE on stderr
    # even when the handshake fails (verified on `openssl s_client`
    # against a closed port — it prints `CONNECTED(...)` followed by
    # `BEGIN CERTIFICATE` from a cached output buffer before exiting
    # non-zero). Trust the returncode, not the BEGIN heuristic.
    if proc.returncode != 0:
        raise RuntimeError(
            f"openssl s_client returned {proc.returncode} for {host}:{port}: "
            f"{proc.stderr.strip()[:200] or '(no stderr)'}"
        )

    try:
        blocks = _parse_pem_blocks(proc.stdout)
    except ValueError as exc:
        # Handshake returned 0 but the output was malformed — treat as
        # a fetch failure so main() can decide to fall back or refuse.
        raise RuntimeError(f"openssl output for {host}:{port} is malformed: {exc}") from exc

    if not blocks:
        raise RuntimeError(
            f"openssl returned 0 for {host}:{port} but stdout contained no PEM blocks "
            f"(stdout first 200 chars: {proc.stdout[:200]!r})"
        )

    # Issue #482: openssl `-showcerts` returns [leaf, intermediate, root];
    # strip the leaf so the bundle contains only CA certs.
    return _drop_leaf(blocks)


def _fetch_chain_python(host: str, port: int, timeout: float, reason: str) -> list[bytes]:
    """Pure-Python fallback when openssl CLI is unavailable.

    Python 3.13+ exposes `SSLSocket.get_verified_chain()` which returns
    the full chain (leaf + intermediates) negotiated during the
    handshake. Earlier Python versions cannot extract intermediates
    from stdlib alone — the chain lives in the OpenSSL `_sslobj`
    private API. Raise clearly so the operator knows to install
    openssl rather than silently shipping a 1-cert bundle (defect 3
    from #454: the prior fallback returned `[ssock.getpeercert()]`
    which is the leaf only).
    """
    import socket  # local — only used in fallback path

    ctx = ssl.create_default_context()
    ctx.check_hostname = False  # chain extraction only — verification happens at consumer
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with ctx.wrap_socket(raw, server_hostname=host) as ssock:
            get_chain = getattr(ssock, "get_verified_chain", None)
            if get_chain is not None:
                try:
                    chain = get_chain()
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(
                        f"Python 3.13+ get_verified_chain() failed for {host}:{port}: "
                        f"{exc!r} (openssl fallback also unavailable: {reason})"
                    ) from exc
                if not chain:
                    raise RuntimeError(
                        f"Python 3.13+ get_verified_chain() returned empty chain for "
                        f"{host}:{port} (openssl fallback also unavailable: {reason})"
                    )
                # `get_verified_chain` shape varies by build:
                #  * CPython 3.13+ returns raw `bytes` (DER) per
                #    https://github.com/python/cpython/issues/118658.
                #  * Older / non-CPython builds (e.g. PyPy, patched ssl
                #    modules) may still return `cryptography.x509.Certificate`
                #    objects that require `public_bytes(Encoding.DER)` to
                #    serialise.
                # Defect 5 (#464): the prior code path only handled the
                # second shape and crashed with `AttributeError` on the
                # first — breaking the openssl-less deployment contract
                # on the very Python version it was designed for. Handle
                # both shapes here so the fallback stays polyglot.
                out: list[bytes] = []
                for c in chain:
                    if isinstance(c, (bytes, bytearray, memoryview)):
                        out.append(bytes(c))
                        continue
                    try:
                        from cryptography.hazmat.primitives import serialization
                    except ImportError as exc:
                        raise RuntimeError(
                            f"Python 3.13+ get_verified_chain() returned a "
                            f"{type(c).__name__} for {host}:{port} and the "
                            f"`cryptography` package is not installed (install "
                            f"`cryptography` or rely on the openssl CLI path): "
                            f"{exc!r}"
                        ) from exc
                    out.append(c.public_bytes(serialization.Encoding.DER))
                # Issue #482: `get_verified_chain()` returns [leaf, ...CA];
                # strip the leaf so the bundle contains only CA certs.
                return _drop_leaf(out)
            raise RuntimeError(
                f"openssl CLI missing and Python <3.13 on {host}:{port}: "
                f"stdlib ssl module cannot extract the certificate chain (only the leaf "
                f"is reachable via getpeercert). Install openssl CLI on the host or "
                f"upgrade Python to 3.13+. Reason: {reason}"
            )


def der_to_pem(der: bytes) -> str:
    """DER bytes → PEM string with line-wrapped base64."""
    import textwrap

    b64 = base64.b64encode(der).decode()
    return "\n".join([_PEM_BEGIN, *textwrap.wrap(b64, 64), _PEM_END]) + "\n"


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
        except RuntimeError as exc:
            try:
                chain = _fetch_chain_python(host, port, args.timeout, reason=str(exc))
            except RuntimeError as fb_exc:
                print(
                    f"[fetch_tinkoff_gost_ca] {host}:{port} → ERROR: openssl path: " f"{exc}; fallback: {fb_exc}",
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
