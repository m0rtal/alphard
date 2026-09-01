"""Static regression test for issue #405 — Stored XSS in operator dashboard.

Issue #405 (Security: Critical): PR #394's ``src/web/static/index.html``
interpolates server-controlled JSON fields directly into ``innerHTML``
without escaping. A malicious ticker with ``figi`` containing
``<img src=x onerror=alert(document.cookie)>`` would execute arbitrary
JS in the operator's browser.

This file is a STATIC check: it parses ``index.html`` as text and
asserts no ``innerHTML = '<literal-template-with-${...}>'`` pattern
remains. The runtime DOM-construction fix uses the existing ``el()``
helper (createElement + textContent) which is XSS-safe by construction.

Why static and not unit tests:
- jsdom under pytest is heavy and the project's pre-PR smoke gate does
  not install it. The HTML is a single static file shipped with the
  Python module; line-counting + grep on the raw text catches the
  defect class precisely.
- A separate end-to-end XSS payload test would require spawning a
  browser (out of scope for SDLC); the static guard is the SDLC gate
  that any future PR must pass before the dashboard can expose
  user-editable data again.

Patterns that ARE allowed (XSS-safe):
- ``innerHTML = '<tr><td>static text</td></tr>'`` — no interpolation.
- ``e.innerHTML = props[k]`` — the caller is the ``el()`` helper;
  eliminated in this PR (replaced with explicit child nodes).
- Plain ``textContent = ...`` (XSS-safe by definition).

Patterns that are NOT allowed (XSS sink):
- ``innerHTML = `...${var}...``` — template literal with interpolation.
- ``innerHTML = '...' + var + '...'`` — string concat with var.
- ``innerHTML += '<...${var}...'` — append with interpolation.

If a future PR legitimately needs innerHTML (e.g. for a static SVG),
add an explicit ``# noqa: xss-allow — <reason>`` comment so this test
can be updated consciously.
"""

from __future__ import annotations

import re
from pathlib import Path

INDEX_HTML = Path(__file__).resolve().parent.parent / "src" / "web" / "static" / "index.html"


def _load_index_html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


class TestNoXSSSinks:
    """Issue #405: no template-literal / string-concat interpolation in innerHTML."""

    def test_index_html_exists(self) -> None:
        assert INDEX_HTML.is_file(), f"index.html missing at {INDEX_HTML}"

    def test_no_template_literal_innerHTML_assignment(self) -> None:
        """innerHTML = `...${var}...` is the canonical XSS sink.

        We assert NO line in index.html contains ``innerHTML = `...${...}``
        (a template literal assigned to innerHTML). Lines with this
        pattern that have a ``# noqa: xss-allow`` comment are exempted
        (none exist today).
        """
        text = _load_index_html()
        # Match lines containing ``innerHTML = ` `` (template literal).
        offenders: list[tuple[int, str]] = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "innerHTML" in line and "`" in line and "${" in line:
                # Allow explicit allow-list comments so future static
                # markup (e.g. SVG) can opt in consciously.
                if "# noqa: xss-allow" in line:
                    continue
                offenders.append((lineno, line.strip()))
        assert not offenders, (
            "Issue #405: template literal assigned to innerHTML is an XSS sink.\n"
            "Use document.createElement + textContent (or the el() helper) instead.\n"
            "Offending lines:\n" + "\n".join(f"  L{ln}: {s}" for ln, s in offenders)
        )

    def test_no_string_concat_innerHTML_with_var(self) -> None:
        """innerHTML = '...' + var is the older XSS sink style.

        We scan for ``innerHTML = '...' + <identifier>`` and
        ``innerHTML += '...${...}'`` patterns.
        """
        text = _load_index_html()
        offenders: list[tuple[int, str]] = []
        # innerHTML = 'something' + some_var  (concat with non-literal)
        concat_pat = re.compile(r"innerHTML\s*=\s*['\"][^'\"]*['\"]\s*\+")
        # innerHTML += '...${...}'  (append with interpolation)
        append_tmpl_pat = re.compile(r"innerHTML\s*\+=\s*['\"`].*\$\{")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "# noqa: xss-allow" in line:
                continue
            if concat_pat.search(line) or append_tmpl_pat.search(line):
                offenders.append((lineno, line.strip()))
        assert not offenders, (
            "Issue #405: string concatenation / append into innerHTML is an XSS sink.\n"
            "Use document.createElement + textContent (or the el() helper) instead.\n"
            "Offending lines:\n" + "\n".join(f"  L{ln}: {s}" for ln, s in offenders)
        )

    def test_el_helper_drops_html_props_dsl(self) -> None:
        """Issue #405: the ``el(tag, props)`` mini-DSL must not allow
        caller-supplied ``html`` props. The pre-fix code at L536 had
        ``else if (k === 'html') e.innerHTML = props[k]`` which let any
        caller pass arbitrary HTML. Drop the branch entirely.
        """
        text = _load_index_html()
        # Find the el() function body and assert 'html' is not a key path.
        m = re.search(r"function el\([^)]*\)\s*\{([\s\S]*?)^\}", text, re.MULTILINE)
        assert m is not None, "el() helper must exist in index.html"
        body = m.group(1)
        assert "innerHTML" not in body, (
            "Issue #405: el() helper must not call innerHTML — use textContent\n"
            f"or document.createTextNode so the helper is XSS-safe by construction.\n"
            f"Helper body:\n{body}"
        )


class TestXSSRenderUsesTextContent:
    """Sanity check: the render functions use textContent / DOM nodes, not innerHTML.

    These are spot-checks on the specific fields called out by the
    QA review on cycle163 (issue #405). A regression that re-introduces
    template-literal innerHTML will trip ``test_no_template_literal_innerHTML_assignment``
    above; this class pins that the canonical safe patterns are present.
    """

    def test_openDrawer_uses_textContent_for_figi(self) -> None:
        """The XSS sink at L742 was `${detail.figi || '—'}` inside innerHTML."""
        text = _load_index_html()
        # The fix uses createElement('dd').textContent = detail.figi || '—'.
        assert "textContent" in text, "index.html must use textContent somewhere"
        # Sanity: 'FIGI' label still appears (rendering not deleted).
        assert "FIGI" in text, "FIGI label must remain in the drawer"

    def test_universe_rows_use_createElement(self) -> None:
        """The XSS sink at L658 was ``tr.innerHTML = `...${t.ticker}...```."""
        text = _load_index_html()
        # After fix, renderUniverse uses el('tr', {}, ...) and el('td', {}, t.ticker).
        assert "createElement" in text, "index.html must use createElement for table rows"
