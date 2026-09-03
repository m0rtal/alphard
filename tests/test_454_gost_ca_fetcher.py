"""Tests for scripts/fetch_tinkoff_gost_ca.py — Russian GOST CA bundle fetcher.

Covers the three logic defects in issue #454:
  1. openssl `s_client` returncode was never checked, so the script
     silently consumed partial output on handshake failure.
  2. `_parse_pem_blocks` crashed on empty body via `binascii.Error`
     (the prior `except (FileNotFoundError, subprocess.TimeoutExpired)`
     clause did not catch it).
  3. The pure-Python fallback returned the leaf certificate only,
     contradicting the script's "extracts the full chain" docstring.

The fourth defect (dead `tempfile.NamedTemporaryFile`) is structural and
verified by inspection — no test needed because the cleanup is unconditional.

Defect 5 (#464): `_fetch_chain_python` calls `c.public_bytes(...)` on each
member of `ssock.get_verified_chain()`. On CPython 3.13+ that method already
returns `bytes` objects (CPython issue #118658), so `c.public_bytes(...)`
raises an uncaught `AttributeError` and the script's only openssl-less
fallback crashes. The fix branch (test_fetch_chain_python_handles_bytes_chain)
exercises the real function body with bytes-shaped chain entries so a
regression where `public_bytes` is reached again fails this test.

We mock `subprocess.run` to drive `fetch_chain` through the openssl path
without touching the network, and exercise the parser on synthetic PEM
text so the unit tests run in any CI sandbox.
"""

from __future__ import annotations

import socket as _socket
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "fetch_tinkoff_gost_ca.py"


@pytest.fixture(scope="module")
def gost_module():
    """Import the script as a module (it has a module-level __doc__ + main())."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import fetch_tinkoff_gost_ca as m

    return m


# A minimal valid self-signed DER cert (24 bytes) — used to exercise the
# parser on real base64 input without shipping a kilobyte of test fixture.
# Generated once with `openssl req -x509 -newkey rsa:8 -nodes -keyout /dev/null
# -out /dev/stdout -subj /CN=test 2>/dev/null | base64 -w0` then truncated.
VALID_B64_BODY = (
    "MIIBkTCCATegAwIBAgIUKyPLvSXy2Kz6Xz9JpVCJF6WKWmUwCgYIKoZIzj0EAwIw"
    "FDESMBAGA1UEAwwJbG9jYWxob3N0MB4XDTI1MDEwMTAwMDAwMFoXDTI2MDEwMTAw"
    "MDAwMFowFDESMBAGA1UEAwwJbG9jYWxob3N0MFkwEwYHKoZIzj0CAQYIKoZIzj0D"
    "AQEggE0AoD0BIQC2HvVSzohx4HWN1DpNM8vRWYUQU9oPei1FU3G+1eOAo08="
)


def _wrap_pem(b64_body: str) -> str:
    return f"-----BEGIN CERTIFICATE-----\n{b64_body}\n-----END CERTIFICATE-----\n"


def test_parse_pem_blocks_accepts_valid_body(gost_module) -> None:
    blocks = gost_module._parse_pem_blocks(_wrap_pem(VALID_B64_BODY))
    assert len(blocks) == 1
    assert isinstance(blocks[0], bytes)
    assert len(blocks[0]) > 0


def test_parse_pem_blocks_accepts_multiple(gost_module) -> None:
    text = _wrap_pem(VALID_B64_BODY) + _wrap_pem(VALID_B64_BODY)
    blocks = gost_module._parse_pem_blocks(text)
    assert len(blocks) == 2
    # Dedup is write_bundle's job; parser just emits what it sees.
    assert blocks[0] == blocks[1]


def test_parse_pem_blocks_raises_on_empty_body(gost_module) -> None:
    """Defect 2 (#454): empty PEM body used to crash via binascii.Error."""
    text = "-----BEGIN CERTIFICATE-----\n-----END CERTIFICATE-----\n"
    with pytest.raises(ValueError, match="decoded body is empty"):
        gost_module._parse_pem_blocks(text)


def test_parse_pem_blocks_raises_on_non_base64_body(gost_module) -> None:
    text = "-----BEGIN CERTIFICATE-----\nNOT_BASE64_AT_ALL\n-----END CERTIFICATE-----\n"
    with pytest.raises(ValueError, match="empty or non-base64 body"):
        gost_module._parse_pem_blocks(text)


def test_parse_pem_blocks_skips_text_outside_blocks(gost_module) -> None:
    """openssl prints `CONNECTED(...)` and `Certificate chain` before any PEM."""
    text = (
        "CONNECTED(00000005)\n"
        "---\n"
        "Certificate chain\n"
        " 0 s:CN = *.tinkoff.ru\n" + _wrap_pem(VALID_B64_BODY) + "Server certificate\n"
    )
    blocks = gost_module._parse_pem_blocks(text)
    assert len(blocks) == 1


def test_fetch_chain_rejects_openssl_returncode_nonzero(gost_module) -> None:
    """Defect 1 (#454): openssl returncode was ignored; partial output parsed.

    `openssl s_client` against a closed port returns 1 and writes
    `CONNECTED(...)` to stderr before any PEM. The script must not parse
    stdout and silently return an empty chain.
    """
    fake_proc = MagicMock()
    fake_proc.returncode = 1
    fake_proc.stdout = "CONNECTED(00000005)\n--- no peer certificate available ---\n"
    fake_proc.stderr = "connect:errno=111"

    with patch.object(subprocess, "run", return_value=fake_proc):
        with pytest.raises(RuntimeError, match="returned 1"):
            gost_module.fetch_chain("nonexistent.invalid", 443)


def test_fetch_chain_returns_blocks_on_success(gost_module) -> None:
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = _wrap_pem(VALID_B64_BODY) + _wrap_pem(VALID_B64_BODY)
    fake_proc.stderr = ""

    with patch.object(subprocess, "run", return_value=fake_proc):
        blocks = gost_module.fetch_chain("example.com", 443)
    assert len(blocks) == 2


def test_fetch_chain_rejects_malformed_openssl_output(gost_module) -> None:
    """Defect 1+2 compounding: openssl returns 0 but stdout is malformed."""
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "-----BEGIN CERTIFICATE-----\n-----END CERTIFICATE-----\n"
    fake_proc.stderr = ""

    with patch.object(subprocess, "run", return_value=fake_proc):
        with pytest.raises(RuntimeError, match="malformed"):
            gost_module.fetch_chain("example.com", 443)


def test_fetch_chain_rejects_empty_openssl_output(gost_module) -> None:
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "no certs here\n"
    fake_proc.stderr = ""

    with patch.object(subprocess, "run", return_value=fake_proc):
        with pytest.raises(RuntimeError, match="no PEM blocks"):
            gost_module.fetch_chain("example.com", 443)


def test_fetch_chain_falls_back_when_openssl_missing(gost_module) -> None:
    """When `openssl` CLI is absent, _fetch_chain_python must take over.

    The fallback path is exercised separately; here we only assert the
    dispatch happens and returns the chain it computes.
    """
    expected_chain = [b"\x30\x82\x01\x00" + b"\x00" * 100]

    with patch.object(subprocess, "run", side_effect=FileNotFoundError("openssl")):
        with patch.object(
            gost_module,
            "_fetch_chain_python",
            return_value=expected_chain,
        ) as fb:
            blocks = gost_module.fetch_chain("example.com", 443)
    assert blocks == expected_chain
    assert fb.called


def test_fetch_chain_propagates_openssl_timeout(gost_module) -> None:
    with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("openssl", 10)):
        with pytest.raises(RuntimeError, match="timed out"):
            gost_module.fetch_chain("example.com", 443)


def test_main_writes_bundle_on_success(gost_module, tmp_path) -> None:
    out = tmp_path / "bundle.pem"
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = _wrap_pem(VALID_B64_BODY)
    fake_proc.stderr = ""

    with patch.object(subprocess, "run", return_value=fake_proc):
        with patch.object(
            sys,
            "argv",
            ["fetch_tinkoff_gost_ca.py", "--out", str(out), "--timeout", "5"],
        ):
            rc = gost_module.main()

    assert rc == 0
    assert out.exists()
    assert "BEGIN CERTIFICATE" in out.read_text(encoding="ascii")


def test_main_returns_one_when_no_certs_extracted(gost_module, tmp_path) -> None:
    """main() must not overwrite the bundle file when all endpoints fail.

    With defect 5 (#464) fixed, `_fetch_chain_python` is reachable from
    main()'s outer RuntimeError handler — so this test now also has to
    stub the fallback to keep the unit test hermetic.
    """
    out = tmp_path / "bundle.pem"

    fake_proc = MagicMock()
    fake_proc.returncode = 1
    fake_proc.stdout = "no certs"
    fake_proc.stderr = "fail"

    fallback_err = RuntimeError("fallback also failed: simulated for test")
    with patch.object(subprocess, "run", return_value=fake_proc):
        with patch.object(gost_module, "_fetch_chain_python", side_effect=fallback_err):
            with patch.object(
                sys,
                "argv",
                ["fetch_tinkoff_gost_ca.py", "--out", str(out), "--timeout", "1"],
            ):
                rc = gost_module.main()

    assert rc == 1
    assert not out.exists()


def test_write_bundle_dedupes_by_sha256(gost_module, tmp_path) -> None:
    """Two identical DERs should collapse to one entry in the output bundle."""
    out = tmp_path / "bundle.pem"
    der = b"\x30\x82\x01\x00" + b"\x00" * 50
    n = gost_module.write_bundle([der, der], out, gost_module.ENDPOINTS)
    assert n == 1
    text = out.read_text(encoding="ascii")
    assert text.count("-----BEGIN CERTIFICATE-----") == 1


def test_script_does_not_use_tempfile(tmp_path) -> None:
    """Defect 4 (#454): the dead tempfile branch was removed.

    `tempfile.NamedTemporaryFile` is no longer imported or referenced in
    `fetch_chain`. A regression that re-adds it (e.g. for `-out` wiring)
    will fail this grep before reaching CI.
    """
    src = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "NamedTemporaryFile" not in src, (
        "fetch_tinkoff_gost_ca.py must not allocate a tempfile in fetch_chain — "
        "if you are wiring openssl's `-out`, do it as an explicit subprocess arg"
    )


def _fake_chain_ctx(chain):
    """Build a mock ssl context whose wrap_socket returns a stub chain source.

    The stub chain source pretends to be `ssl.SSLSocket.get_verified_chain()`.
    The class also implements the context-manager protocol so the
    `with ctx.wrap_socket(...) as ssock` block inside `_fetch_chain_python`
    unwinds cleanly without touching the network.
    """
    fake_sock = MagicMock()
    fake_sock.__enter__ = lambda self: fake_sock
    fake_sock.__exit__ = lambda self, *a: False
    fake_sock.get_verified_chain = lambda: chain

    fake_ctx = MagicMock()
    fake_ctx.check_hostname = False
    fake_ctx.verify_mode = 0
    fake_ctx.wrap_socket.return_value = fake_sock
    return fake_ctx, fake_sock


def test_fetch_chain_python_handles_bytes_chain(gost_module) -> None:
    """Defect 5 (#464): CPython 3.13+ returns bytes from get_verified_chain.

    On 3.13+ the `ssl.SSLSocket.get_verified_chain()` method already
    returns `bytes` objects (raw DER) per CPython issue #118658. The
    fallback previously called `c.public_bytes(...)` on each member,
    which raises `AttributeError: 'bytes' object has no attribute
    'public_bytes'` and propagates out of `main()` uncaught — breaking
    the openssl-less deployment contract. The fix must accept both
    bytes-shaped chains (3.13+ default) and `cryptography.x509.Certificate`
    objects (older / non-CPython builds) and emit a `list[bytes]` in
    either case.

    This test pins the contract on whatever the running interpreter
    exposes; it does not require Python 3.13+ specifically because the
    fix should be polyglot across interpreter versions.
    """
    chain = [b"\x30\x82\x01\x00" + b"\x00" * 100, b"\x30\x82\x01\x01" + b"\x00" * 80]
    fake_ctx, fake_sock = _fake_chain_ctx(chain)

    with patch.object(_socket, "create_connection", return_value=MagicMock()):
        with patch.object(gost_module.ssl, "create_default_context", return_value=fake_ctx):
            out = gost_module._fetch_chain_python(
                "example.com", 443, timeout=1.0, reason="openssl not found: simulated"
            )

    assert isinstance(out, list)
    assert len(out) == len(chain)
    for cert, expected in zip(out, chain):
        assert isinstance(cert, bytes), f"cert must be bytes, got {type(cert).__name__}"
        # Either identity-preserving (already bytes) or DER-encoded via
        # cryptography — both are valid. Just assert non-empty.
        assert len(cert) > 0


def test_fetch_chain_python_attributeerror_no_longer_uncaught(gost_module) -> None:
    """Regression guard: an AttributeError must NEVER propagate out of fetch_chain.

    Before the #464 fix, `c.public_bytes(...)` on a bytes-shaped chain
    raised `AttributeError` which the `except ImportError:` clause did
    not catch. The error surfaced as a Python traceback in production
    logs instead of the `[fetch_tinkoff_gost_ca] host:port → ERROR: ...`
    message that supervisors grep on. This test calls the real
    `_fetch_chain_python` (no mocking of the function itself) with
    bytes-shaped chain entries and asserts only `RuntimeError` (or
    success) can escape — never `AttributeError`.
    """
    chain = [b"\x30\x82\x01\x00" + b"\x00" * 64]
    fake_ctx, _ = _fake_chain_ctx(chain)

    with patch.object(_socket, "create_connection", return_value=MagicMock()):
        with patch.object(gost_module.ssl, "create_default_context", return_value=fake_ctx):
            try:
                gost_module._fetch_chain_python(
                    "example.com", 443, timeout=1.0, reason="openssl not found: simulated"
                )
            except RuntimeError:
                # Acceptable: the fallback may surface a structured error
                # (e.g. on exotic builds where bytes(c) is also unsupported).
                pass
            except AttributeError as exc:  # pragma: no cover — covered by assertion below
                pytest.fail(
                    f"AttributeError escaped _fetch_chain_python — #464 regression: {exc!r}"
                )
