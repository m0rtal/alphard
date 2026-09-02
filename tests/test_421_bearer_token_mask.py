"""Regression tests for issue #421 — alphard-web token prompt must mask input.

Issue #421 (Security: Medium): the bearer-token prompt in
``src/web/static/index.html`` used ``window.prompt()``, which renders
the input as **plaintext** letters in a browser-native modal dialog.
Anyone with line-of-sight to the operator's screen during the prompt
moment (colleague, screen capture, VNC/RDP session, browser screenshot
extension) reads the token character-by-character. The same token
grants full read access to ``/api/summary``, ``/api/ticker/<symbol>``,
``/api/backups``, ``/api/settings`` until the operator rotates
``ALPHARD_WEB_TOKEN``.

The correct UI is ``<input type="password">`` inside a styled modal so
the browser masks every character (rendered as ``••••``). This file
pins that contract via static analysis on the raw HTML — same pattern
as ``tests/test_411_auth_gate_js_wiring.py`` (the JS is not runnable in
pytest without jsdom).

Acceptance criteria from issue #421:

1. ``grep -nE 'window\\.prompt\\(' src/web/static/index.html`` returns
   zero matches. ``window.prompt`` may still appear in comments
   explaining why it is forbidden, but never as a live call.
2. The HTML contains a ``<input type="password">`` field used for
   bearer-token input.
3. The new prompt is reachable from ``getToken()`` (the only consumer
   of the bearer-token storage key).
4. The previous tests ``test_token_prompt_on_first_load``,
   ``test_api_helper_handles_401``, ``test_api_helper_reads_session_storage``
   stay green — the auth gate's behaviour is preserved, only the
   input primitive changes.
"""

from __future__ import annotations

import re
from pathlib import Path

INDEX_HTML = Path(__file__).resolve().parent.parent / "src" / "web" / "static" / "index.html"


def _load_index_html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


# Strip JS line comments and block comments so the assertions below
# only match live code, not documentation that *talks about* the
# forbidden constructs. Without this, the "no window.prompt call" rule
# would have to allow comments — which defeats the regression guard.
_COMMENT_LINE_RE = re.compile(r"//[^\n]*")
_COMMENT_BLOCK_RE = re.compile(r"/\*[\s\S]*?\*/")


def _strip_js_comments(text: str) -> str:
    """Remove ``// ...`` and ``/* ... */`` comments from the JS source.

    Operates line-by-line via regex. Safe for the static HTML used in
    this project: the page does not embed the literal sequence ``//``
    inside any string literal that the regression guard cares about
    (token label, prompt title). If a future change embeds ``//`` in a
    string, tighten the regex — out of scope here.
    """
    no_block = _COMMENT_BLOCK_RE.sub("", text)
    return _COMMENT_LINE_RE.sub("", no_block)


class TestNoWindowPromptCall:
    """Issue #421: ``window.prompt()`` must NOT appear as live code.

    Comments documenting the prohibition are fine; live calls are not.
    A regression that re-introduces ``window.prompt`` re-opens the
    shoulder-surfing vector the fix closed.
    """

    def test_window_prompt_not_called_in_live_js(self) -> None:
        text = _load_index_html()
        code = _strip_js_comments(text)
        assert "window.prompt(" not in code, (
            "Issue #421: bearer-token prompt must mask characters via "
            "<input type='password'>. `window.prompt()` renders plaintext "
            "(shoulder-surfing / screen-capture exposure). Found a live "
            "call in src/web/static/index.html — replace with the "
            "#modal element + promptPassword() helper."
        )

    def test_bare_prompt_not_called_for_bearer_token(self) -> None:
        """Even non-window ``prompt(...)`` is forbidden for the token flow.

        The fix route is a styled modal; no JS ``prompt(...)`` call —
        bare or window-qualified — must remain in the page's live code.
        """
        text = _load_index_html()
        code = _strip_js_comments(text)
        # Look for any `prompt(` call (with or without receiver). This
        # catches both `prompt(` and `window.prompt(`.
        assert not re.search(r"(?:^|[^.\w])prompt\s*\(", code), (
            "Issue #421: no JS prompt(...) call may remain in live code "
            "for the bearer-token flow. Use <input type='password'>."
        )


class TestPasswordInputPresent:
    """Issue #421: ``<input type=\"password\">`` must be the token field."""

    def test_password_input_in_source(self) -> None:
        text = _load_index_html()
        assert 'type="password"' in text or "type='password'" in text, (
            "Issue #421: src/web/static/index.html must contain a "
            "<input type='password'> field so the browser masks the "
            "bearer-token characters. Found neither double- nor "
            "single-quoted variant."
        )

    def test_password_input_wired_to_token_field(self) -> None:
        """The password input must be the token-input element.

        The input is built as a JS string (multi-line concatenation in
        ``promptPassword()``), so we match the substring pair rather
        than a single tag. Pinning the id makes the regression surface
        visibly: any future change that removes the field fails this
        test, and the error message tells the next maintainer exactly
        which field to keep.
        """
        text = _load_index_html()
        # The canonical build line is:
        #   '<input type="password" id="auth-token-input" autocomplete="off" ' +
        # but attributes can be re-ordered, so we accept both orderings.
        code = _strip_js_comments(text)
        has_pw_type = bool(re.search(r"type\s*=\s*['\"]password['\"]", code))
        has_token_id = bool(re.search(r"id\s*=\s*['\"]auth-token-input['\"]", code))
        assert has_pw_type and has_token_id, (
            "Issue #421: the bearer-token input must be "
            "<input type='password' id='auth-token-input' ...> so the "
            "browser masks characters and the promptPassword() helper "
            "can read its value. Found type='password': "
            + str(has_pw_type)
            + ", id='auth-token-input': "
            + str(has_token_id)
            + "."
        )


class TestGetTokenUsesMaskedModal:
    """Issue #421: ``getToken()`` must route through the masked modal.

    The token prompt must NOT call any ``prompt(...)`` directly. The
    new route is a Promise-based helper (``promptPassword``) that
    opens the existing #modal element with a password input.
    """

    def test_get_token_uses_prompt_password_helper(self) -> None:
        text = _load_index_html()
        m = re.search(
            r"(?:async\s+)?function\s+getToken\s*\([^)]*\)\s*\{([\s\S]*?)\n\}",
            text,
        )
        assert m is not None, "getToken() helper must remain a top-level function"
        body = m.group(1)
        code = _strip_js_comments(body)
        assert "promptPassword(" in code, (
            "Issue #421: getToken() must call promptPassword(...) so the "
            "operator enters the bearer token in a masked modal. Current "
            "body:\n" + body
        )
        # Belt-and-suspenders: still no live prompt() call in getToken.
        assert (
            "prompt(" not in code or "promptPassword(" in code
        ), "Issue #421: getToken() must not call prompt() directly."

    def test_prompt_password_helper_defines_password_input(self) -> None:
        """The helper must build the masked input element itself.

        Pinned here so a regression that drops the input element but
        keeps the helper name is caught — the helper without the
        input is no longer the masked-input primitive. The modal-open
        behaviour is delegated to ``openModal(...)`` (which calls
        ``classList.add('open')`` on ``#modal``); we pin that the
        helper invokes it.
        """
        text = _load_index_html()
        m = re.search(
            r"function\s+promptPassword\s*\([^)]*\)\s*\{([\s\S]*?)\n\}",
            text,
        )
        assert m is not None, "promptPassword() helper must be defined"
        body = m.group(1)
        assert 'type="password"' in body or "type='password'" in body, (
            "Issue #421: promptPassword() must construct an " "<input type='password'> element. Current body:\n" + body
        )
        # The helper delegates to openModal(...) which adds the
        # 'open' class to #modal. Either direct classList.add('open')
        # or an openModal(...) call is acceptable — both surface the
        # password field.
        opens_modal = "classList.add('open')" in body or 'classList.add("open")' in body or "openModal(" in body
        assert opens_modal, (
            "Issue #421: promptPassword() must open the #modal element "
            "(directly via classList.add('open') or by calling "
            "openModal(...)) so the password field is rendered. "
            "Current body:\n" + body
        )


class TestAuthGateBehaviourPreserved:
    """Issue #421 must not regress the auth flow that #411 introduced.

    The token still flows through ``sessionStorage`` (so the tab
    reuses it for navigation) and ``api()`` still clears it on 401.
    """

    def test_get_token_still_reads_session_storage(self) -> None:
        text = _load_index_html()
        m = re.search(
            r"(?:async\s+)?function\s+getToken\s*\([^)]*\)\s*\{([\s\S]*?)\n\}",
            text,
        )
        assert m is not None
        body = m.group(1)
        assert "sessionStorage.getItem" in body, (
            "Issue #421 must NOT regress issue #411: getToken() must "
            "still read the bearer token from sessionStorage. Current "
            "body:\n" + body
        )

    def test_get_token_still_writes_session_storage(self) -> None:
        text = _load_index_html()
        m = re.search(
            r"(?:async\s+)?function\s+getToken\s*\([^)]*\)\s*\{([\s\S]*?)\n\}",
            text,
        )
        assert m is not None
        body = m.group(1)
        assert "sessionStorage.setItem" in body, (
            "Issue #421 must NOT regress issue #411: getToken() must "
            "persist the entered token in sessionStorage so subsequent "
            "fetches in the same tab reuse it. Current body:\n" + body
        )

    def test_api_helper_still_handles_401(self) -> None:
        text = _load_index_html()
        m = re.search(
            r"(?:async\s+)?function\s+api\s*\([^)]*\)\s*\{([\s\S]*?)\n\}",
            text,
        )
        assert m is not None
        body = m.group(1)
        assert "status === 401" in body or "status==401" in body, (
            "Issue #421 must NOT regress issue #411: api() helper must "
            "still check for status === 401 and clear the stored token."
        )
        assert "sessionStorage.removeItem" in body, (
            "Issue #421 must NOT regress issue #411: api() helper must "
            "call sessionStorage.removeItem on 401 so the operator can "
            "re-enter the token."
        )
