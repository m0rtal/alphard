"""Regression tests for issue #413 — el() helper on* event-handler sink.

Issue #413 (Tech-Debt: Medium): ``src/web/static/index.html`` defines a
``el(tag, props, ...children)`` mini-DSL. The ``else if (k.startsWith('on'))
e[k] = props[k]`` branch assigns event-handler properties to DOM nodes
directly. If a future maintainer ever calls ``el('div', someServerObject)``
where ``someServerObject = {"onclick": "alert(1)", ...}``, the browser
will execute the string ``alert(1)`` as JavaScript via DOM's implicit
``Function``-constructor coercion on ``on*`` properties.

The class is XSS-by-string-coercion: the current XSS test in
``tests/test_web_xss_static.py::TestNoXSSSinks::test_el_helper_drops_html_props_dsl``
only blocks ``innerHTML`` inside ``el()``, not the ``on*`` branch.

Today, all 14 calls to ``el()`` in the dashboard pass literal hard-coded
prop keys ('data-ticker', 'class', 'num', ...). So the sink is NOT
currently reachable. But it is one line away from being reachable if
a future PR mirrors the existing pattern of
``el('td', {}, t.ticker)`` into ``el('td', t)`` — passing the server
row object directly.

The fix has two coordinated parts:

1. **Drop the ``on*`` branch entirely.** Callers that need event
   handlers must set them on the returned element after ``el()`` returns
   so the handler is always a function literal — server data cannot
   smuggle in a string-coerced handler.

2. **Extend the static XSS guard** with a new ``TestElHelperRejectsDynamicHandlers``
   class that pins the contract.

Same pattern as ``tests/test_web_xss_static.py``: static analysis on
the raw text, no jsdom required.
"""

from __future__ import annotations

import re
from pathlib import Path

INDEX_HTML = Path(__file__).resolve().parent.parent / "src" / "web" / "static" / "index.html"


def _load_index_html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _el_helper_body() -> str:
    """Return the body of the ``el()`` helper function.

    Raises AssertionError if the helper is missing.
    """
    text = _load_index_html()
    m = re.search(r"function\s+el\s*\([^)]*\)\s*\{([\s\S]*?)\n\}", text)
    assert m is not None, "el() helper must exist in index.html"
    return m.group(1)


class TestElHelperRejectsDynamicHandlers:
    """Issue #413: el() must not accept ``on*`` event-handler props.

    The DOM property setter for ``onclick`` / ``onload`` / etc. coerces
    string values into functions via the ``Function`` constructor. A
    server-controlled row object that contains ``{"onclick": "alert(1)"}``
    would execute arbitrary JS on click. The only safe contract is to
    refuse ``on*`` props entirely and force callers to attach handlers
    after ``el()`` returns (where the handler is a function literal).
    """

    def test_el_helper_does_not_branch_on_startswith_on(self) -> None:
        """Issue #413: ``else if (k.startsWith('on'))`` branch must be gone.

        The branch is the canonical XSS-via-property-coercion sink.
        """
        body = _el_helper_body()
        # Find any line with startsWith('on') inside the helper body.
        offenders = [line.strip() for line in body.splitlines() if "startsWith" in line and "'on'" in line]
        assert not offenders, (
            "Issue #413: el() helper must NOT branch on "
            "k.startsWith('on'). Drop the branch — callers attach handlers "
            "after el() returns. Offending lines:\n" + "\n".join(f"  {o}" for o in offenders)
        )

    def test_el_helper_does_not_assign_event_handlers_via_property(self) -> None:
        """Issue #413: ``e[k] = props[k]`` is the assignment sink.

        Combined with the ``on*`` branch, it lets server data coerce a
        string into a function. Both pieces must be gone.
        """
        body = _el_helper_body()
        # Look for property assignment patterns inside the props loop.
        # Acceptable: e.className = props[k], e.setAttribute(...)
        # Not acceptable: e[k] = props[k], e.onclick = props[k], e.on* = props[k]
        offenders = []
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("//"):
                continue
            # e[k] = ... is the dynamic property sink.
            if re.match(r"e\[\s*k\s*\]\s*=", stripped):
                offenders.append(stripped)
            # e.onclick = ... etc. are literal event-handler assignments.
            if re.match(r"e\.on[a-z]+\w*\s*=", stripped):
                offenders.append(stripped)
        assert not offenders, (
            "Issue #413: el() helper must NOT assign to e[k] or e.on*. "
            "Use e.className, e.setAttribute, or attach handlers after "
            "el() returns. Offending lines:\n" + "\n".join(f"  {o}" for o in offenders)
        )

    def test_el_helper_only_assigns_class_and_setattribute(self) -> None:
        """Positive contract: el() assigns ONLY to e.className + setAttribute.

        After the fix, the only prop-side mutations are:
        - e.className = props[k]   (for k === 'class')
        - e.setAttribute(k, props[k])  (default branch)
        """
        body = _el_helper_body()
        # Find the props loop and confirm it only does className + setAttribute.
        # Look for any assignment to `e.<something>` in the body.
        prop_assignments = []
        for line in body.splitlines():
            stripped = line.strip()
            if re.match(r"e\.\w+\s*=", stripped):
                prop_assignments.append(stripped)
        allowed = {"e.className = props[k]"}
        unexpected = [a for a in prop_assignments if a not in allowed]
        assert not unexpected, (
            "Issue #413: el() helper must only assign to e.className from props. "
            "Found other assignments:\n" + "\n".join(f"  {u}" for u in unexpected)
        )

    def test_no_el_call_site_uses_on_handler_props(self) -> None:
        """Regression guard: no caller passes ``{onclick: ...}`` to el().

        Today there are no such callers (verified by grep). If a future
        PR introduces one, this test fails — forcing the author to use
        the post-el() assignment pattern instead.
        """
        text = _load_index_html()
        offenders = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            # el(..., { on*: ... }) is the dangerous pattern.
            if re.search(r"el\s*\([^)]*\{\s*on[a-z]+\s*:", line):
                offenders.append((lineno, line.strip()))
        assert not offenders, (
            "Issue #413: no el() call site may pass on* props. "
            "Attach the handler after el() returns:\n"
            "  const btn = el('button', {'data-x': 1}, '...');\n"
            "  btn.onclick = () => doSomething();\n"
            "Offending lines:\n" + "\n".join(f"  L{ln}: {s}" for ln, s in offenders)
        )

    def test_no_el_call_site_spreads_server_object(self) -> None:
        """Regression guard: no caller passes a server row object directly.

        The dangerous pattern is ``el('td', t)`` where ``t`` is the
        JSON row. We pin that every el() call passes either a literal
        object, an empty object, or a known-safe literal prop key.
        """
        text = _load_index_html()
        # Pattern: el('tag', IDENT)  — the IDENT is the second positional arg
        # without a `{`. Server row objects appear as `el('td', t)` etc.
        offenders = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            # el('tag', someVar)  without `{` after the comma.
            m = re.search(r"\bel\s*\(\s*['\"][^'\"]+['\"]\s*,\s*([a-zA-Z_][\w.]*)\s*\)", line)
            if m:
                # Only flag if the second arg is NOT an inline object literal.
                # (A `{` after the comma means it's an object, not a variable.)
                offenders.append((lineno, m.group(1), line.strip()))
        assert not offenders, (
            "Issue #413: el() must not be called with a bare server-row "
            "variable as props. Pass an explicit object literal so the "
            "set of prop keys is statically auditable.\n"
            "Offending lines:\n" + "\n".join(f"  L{ln} (arg={a}): {s}" for ln, a, s in offenders)
        )
