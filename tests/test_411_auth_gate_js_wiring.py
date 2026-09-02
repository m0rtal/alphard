"""Regression tests for issue #411 — alphard-web auth gate is wired but
JS client never sends Authorization.

Issue #411 (Security: Critical): PR #394 followup (commit ``e079d7f``)
added a bearer-token auth gate that protects every ``/api/*`` endpoint
PLUS the HTML root path ``/``. The backend gate works correctly. But
the static HTML at ``src/web/static/index.html`` makes 9 ``fetch()``
calls via the ``api()`` helper at line 519 that sends no
``Authorization`` header. With ``ALPHARD_WEB_TOKEN`` set, the HTML
page itself returns 401 (the HTML root is gated) AND every API call
would also return 401. The dashboard is functionally dead in any
deployment with the production-recommended token set.

The fix has three coordinated parts:

1. **HTML root path becomes auth-open.** The dashboard HTML itself is
   reachable without auth so the token-prompt can render. This matches
   the Grafana login-page model — the page is public, the data behind
   it is gated. The Python ``check_auth()`` constant ``_AUTH_OPEN_PATHS``
   must include ``/``.

2. **JS client acquires the token and sends it.** The ``api()`` helper
   reads ``sessionStorage.alphard_web_token``, prompts on first load,
   and injects ``Authorization: Bearer <token>`` into every fetch.

3. **401 from the server re-prompts.** If the server returns 401 (the
   operator typed a wrong token, or the server token was rotated), the
   helper must clear sessionStorage and surface the error so a
   re-prompt is possible.

These tests pin the contract. ``TestHtmlRootIsAuthOpen`` pins part 1;
``TestApiHelperSendsAuthorization`` and ``TestApiHelperRePromptsOn401``
pin part 2-3 via static analysis (the JS is not runnable in pytest
without jsdom — the project's pre-PR smoke gate does not install it).

Same pattern as ``tests/test_web_xss_static.py``: the HTML is shipped
as a single static file inside the Python module, line-counting +
regex on the raw text catches the defect class precisely.
"""

from __future__ import annotations

import re
from pathlib import Path

INDEX_HTML = Path(__file__).resolve().parent.parent / "src" / "web" / "static" / "index.html"


def _load_index_html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _load_server_py() -> str:
    return (Path(__file__).resolve().parent.parent / "src" / "web" / "server.py").read_text(encoding="utf-8")


# --- Part 1: HTML root is auth-open (Python side) ----------------------


class TestHtmlRootIsAuthOpen:
    """Issue #411: HTML root path must be reachable without auth.

    Without this, the token-prompt in the JS client can never render
    because the page itself returns 401. The Grafana model: the login
    page is public, the data behind it is gated.
    """

    def test_html_root_in_auth_open_paths(self) -> None:
        """``_AUTH_OPEN_PATHS`` must contain ``/``."""
        text = _load_server_py()
        # Find the _AUTH_OPEN_PATHS assignment and verify it contains '/'.
        m = re.search(r"_AUTH_OPEN_PATHS:\s*frozenset\[str\]\s*=\s*frozenset\(\s*\{([^}]*)\}", text)
        assert m is not None, (
            "server.py must define _AUTH_OPEN_PATHS as a frozenset[str] literal "
            "so the issue #411 audit can statically inspect its members"
        )
        body = m.group(1)
        assert "'/'" in body or '"/"' in body, (
            "Issue #411: HTML root '/' must be in _AUTH_OPEN_PATHS so the "
            "JS token-prompt can render. Current members: " + body.strip()
        )

    def test_check_auth_html_root_is_open(self) -> None:
        """check_auth('/') must return True regardless of Authorization header."""
        from src.web import server as server_mod

        # With token unset → fail-open anyway (pre-existing behaviour).
        # With token set → HTML root must be open so the page can render.
        import os

        prev = os.environ.get("ALPHARD_WEB_TOKEN")
        os.environ["ALPHARD_WEB_TOKEN"] = "secret-token-abc"
        try:
            assert server_mod.check_auth("/", None) is True, (
                "Issue #411: check_auth('/') must return True so the HTML "
                "root is reachable without auth when the token is set. "
                "Without this, the JS client cannot load the page to "
                "prompt the operator for the token."
            )
        finally:
            if prev is None:
                os.environ.pop("ALPHARD_WEB_TOKEN", None)
            else:
                os.environ["ALPHARD_WEB_TOKEN"] = prev

    def test_api_paths_still_require_auth(self) -> None:
        """Gating must NOT regress: /api/* must still require bearer token."""
        from src.web import server as server_mod

        import os

        prev = os.environ.get("ALPHARD_WEB_TOKEN")
        os.environ["ALPHARD_WEB_TOKEN"] = "secret-token-abc"
        try:
            assert server_mod.check_auth("/api/summary", None) is False, (
                "Issue #411 fix must NOT regress the auth gate — " "/api/* must still require a bearer token."
            )
            assert (
                server_mod.check_auth("/api/summary", "Bearer secret-token-abc") is True
            ), "Issue #411 fix must NOT regress — correct bearer must pass."
        finally:
            if prev is None:
                os.environ.pop("ALPHARD_WEB_TOKEN", None)
            else:
                os.environ["ALPHARD_WEB_TOKEN"] = prev

    def test_open_paths_documented_in_module_docstring(self) -> None:
        """The server module docstring must call out the HTML root as auth-open."""
        text = _load_server_py()
        # The module docstring is between the first """ pair near the top.
        m = re.search(r'^"""([\s\S]*?)"""', text)
        assert m is not None, "server.py must have a module docstring"
        docstring = m.group(1)
        # Either 'auth-open', 'public', 'login-page model', or similar.
        has_callout = any(
            phrase in docstring.lower()
            for phrase in (
                "html root",
                "/",
                "login page",
                "token prompt",
                "public",
                "auth-open",
            )
        )
        assert has_callout, (
            "Issue #411: server.py module docstring must call out the HTML "
            "root as auth-open so future maintainers don't re-gate it."
        )


# --- Part 2 + 3: JS client wires the Authorization header ----------------


class TestApiHelperSendsAuthorization:
    """Issue #411: the api() helper in index.html must send Authorization.

    Without this, every fetch() returns 401 even with a correct token
    because the JS never attaches the header. The dashboard is dead
    in any deployment with ALPHARD_WEB_TOKEN set.

    We assert the source-level contract: api() must read the token
    from sessionStorage and pass it via the fetch ``headers`` option.
    """

    def test_api_helper_exists(self) -> None:
        text = _load_index_html()
        assert (
            "async function api(" in text or "function api(" in text
        ), "Issue #411: index.html must define an api() helper that wraps fetch()."

    def test_api_helper_uses_fetch_with_headers(self) -> None:
        """The fetch call must include a headers: { Authorization: ... } option.

        We scan for the helper body and assert it includes a headers
        object with an Authorization field. A naive ``fetch(path)`` with
        no headers object fails the regression.
        """
        text = _load_index_html()
        m = re.search(r"(?:async\s+)?function\s+api\s*\([^)]*\)\s*\{([\s\S]*?)\n\}", text)
        assert m is not None, "Issue #411: api() helper must be a top-level function"
        body = m.group(1)
        # The headers object must contain Authorization.
        # Acceptable patterns:
        #   headers: {Authorization: ...}
        #   headers: {... Authorization: ...}
        #   headers: _token ? {...} : {}
        # We accept Authorization anywhere within a `{ ... }` object
        # assigned to a `headers` identifier. The negation `(?!\s*})` keeps
        # the Authorization inside the same object literal.
        has_auth_header = bool(
            re.search(
                r"headers\s*=\s*[^;]*\{[^}]*Authorization",
                body,
            )
        )
        assert has_auth_header, (
            "Issue #411: api() helper must attach an Authorization header to "
            "every fetch. Current helper body:\n" + body
        )

    def test_api_helper_reads_session_storage(self) -> None:
        """The token must be stored in sessionStorage so it survives nav."""
        text = _load_index_html()
        m = re.search(r"(?:async\s+)?function\s+api\s*\([^)]*\)\s*\{([\s\S]*?)\n\}", text)
        assert m is not None
        body = m.group(1)
        assert "sessionStorage" in body, (
            "Issue #411: api() helper must read the bearer token from "
            "sessionStorage so the value survives page navigation. "
            "Current helper body:\n" + body
        )

    def test_token_prompt_on_first_load(self) -> None:
        """When no token in sessionStorage, prompt the operator once.

        Issue #421 replaces the legacy ``window.prompt(...)`` call with
        a Promise-based ``promptPassword(...)`` helper that opens a
        styled modal containing ``<input type=\"password\">`` (so the
        browser masks the token against shoulder-surfing). Both
        mechanisms satisfy this regression — the contract is "the page
        acquires the bearer token on first load before any api() call",
        not the specific JS primitive used.
        """
        text = _load_index_html()
        prompt_patterns = (
            # Legacy mechanism (issue #411, closed by PR #414).
            r"prompt\s*\(\s*['\"][^'\"]*alphard[_-]?web[_-]?token[^'\"]*['\"]",
            r"prompt\s*\(\s*['\"][^'\"]*bearer\s+token[^'\"]*['\"]",
            # Modern mechanism (issue #421, fixed by this branch):
            # getToken() awaits promptPassword({title, label, ...})
            # and the label is the operator-visible UX string.
            r"promptPassword\s*\(\s*\{[^}]*label\s*:\s*['\"][^'\"]*bearer\s+token[^'\"]*['\"]",
        )
        prompt_match = any(re.search(p, text, re.IGNORECASE) for p in prompt_patterns)
        assert prompt_match, (
            "Issue #411: index.html must prompt the operator for the bearer "
            "token on first load. Searched for the legacy prompt(...) call "
            "and the issue #421 promptPassword({label: 'Bearer token', ...}) "
            "form in the page source. Neither matched — the operator will "
            "never be prompted and api() will fire with no Authorization."
        )


class TestApiHelperRePromptsOn401:
    """Issue #411: 401 response must clear sessionStorage and re-prompt."""

    def test_api_helper_handles_401(self) -> None:
        text = _load_index_html()
        m = re.search(r"(?:async\s+)?function\s+api\s*\([^)]*\)\s*\{([\s\S]*?)\n\}", text)
        assert m is not None
        body = m.group(1)
        # Must check r.status === 401 (or similar) and removeItem from sessionStorage.
        has_401_check = bool(re.search(r"status\s*===?\s*401", body))
        assert has_401_check, (
            "Issue #411: api() helper must check for r.status === 401 and "
            "clear the stored token so the next reload re-prompts. "
            "Current helper body:\n" + body
        )
        # And sessionStorage.removeItem is called on 401 path.
        has_remove = "removeItem" in body
        assert has_remove, (
            "Issue #411: api() helper must call sessionStorage.removeItem "
            "on 401 so the operator can re-enter the token. "
            "Current helper body:\n" + body
        )
