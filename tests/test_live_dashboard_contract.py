"""Browser-free contract tests for the live dashboard's hand-synced couplings.

The dashboard (``src/viewlyt/live/dashboard``: ``index.html`` + ``src/main.ts`` +
``src/style.css``, vanilla TS built by Vite) is coupled to the HTML, to the
committed build output (``src/viewlyt/live/static``) and to Python
(``llm.py``/``probes.py``) purely BY HAND -- nothing in the toolchain notices
when one side drifts, and the failure mode is silent (``el("x")`` throws and
aborts the whole bootstrap; a stale bundle ships; a provider can be selected in
the UI that the server rejects...). These tests read the files at test time, so
they keep working as the UI evolves, and need no node, browser or network.

Set ``VIEWLYT_CONTRACT_BASE`` to a directory holding ``dashboard/`` and
``static/`` to point every file read at a scratch copy (used to prove the tests
bite: mutate the copy, expect a failure). The Python side (``llm``/``probes``)
is always the real, imported one.
"""

from __future__ import annotations

import os
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

pytest.importorskip("fastapi")  # the live package needs the `live` extra

from viewlyt.live import llm, probes  # noqa: E402

_REPO = Path(__file__).resolve().parents[1]
BASE = Path(os.environ.get("VIEWLYT_CONTRACT_BASE") or _REPO / "src" / "viewlyt" / "live")
DASH = BASE / "dashboard"
STATIC = BASE / "static"
MAIN_TS = DASH / "src" / "main.ts"
SRC_HTML = DASH / "index.html"
BUILT_HTML = STATIC / "index.html"
ASSETS = STATIC / "assets"
E2E_TEST = _REPO / "tests" / "test_live_e2e.py"

# Ids main.ts reads with a LITERAL lookup but that exist in neither index.html,
# because main.ts (or something else at runtime) creates them. Detected
# automatically when main.ts assigns the id itself; add an id here ONLY when that
# detection cannot see it, and say why.
IDS_NOT_IN_HTML: dict[str, str] = {}

# Selectors the browser e2e (tests/test_live_e2e.py) relies on, besides whatever
# is parsed out of that file at test time.
E2E_IDS = (
    "status",
    "feed",
    "ingested",
    "probe-label",
    "probe-instruction",
    "add-probe",
    "toasts",
)
E2E_CLASSES = ("result-card", "toast-success", "toast-error", "empty-hint")


# ---------------------------------------------------------------------------
# Reading / scanning helpers
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text(encoding="utf-8")


def strip_js_comments(src: str) -> str:
    """Blank out ``//`` and ``/* */`` comments, keeping strings and newlines.

    A tiny state machine (not a parser): tracks '...', "..." and `...` strings so a
    ``//`` inside a URL string is not mistaken for a comment, and keeps every
    newline so line numbers computed on the result match the original file.
    Regex literals are not tracked; ``//`` can only appear in one as ``\\/\\/``
    (escaped) which we skip via the backslash rule, so this is safe in practice.
    """
    out: list[str] = []
    i, n = 0, len(src)
    quote = ""
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(nxt)
                i += 2
                continue
            if c == quote:
                quote = ""
        elif c in "\"'`":
            quote = c
            out.append(c)
        elif c == "\\" and i + 1 < n:  # escape outside a string (regex literal)
            out.append(c + nxt)
            i += 2
            continue
        elif c == "/" and nxt == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        elif c == "/" and nxt == "*":
            end = src.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append(re.sub(r"[^\n]", " ", src[i:end]))
            i = end
            continue
        else:
            out.append(c)
        i += 1
    return "".join(out)


def line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


class _IdCollector(HTMLParser):
    """Collects every ``id="..."`` attribute of an HTML document."""

    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.options: dict[str, list[str]] = {}  # <select id> -> option values
        self._select: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if a.get("id"):
            self.ids.append(a["id"] or "")
        if tag == "select":
            self._select = a.get("id") or ""
            self.options[self._select] = []
        elif tag == "option" and self._select is not None:
            self.options[self._select].append(a.get("value") or "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self._select = None


def parse_html(path: Path) -> _IdCollector:
    p = _IdCollector()
    p.feed(_read(path))
    return p


def html_ids(path: Path) -> set[str]:
    return set(parse_html(path).ids)


def balanced_block(text: str, open_idx: int) -> str:
    """Return ``text[open_idx : matching close + 1]`` for the ``{``/``[`` at open_idx.

    Brace-counting that skips string/template literals, so a ``}`` inside a string
    does not end the block early.
    """
    pairs = {"{": "}", "[": "]"}
    open_ch = text[open_idx]
    close_ch = pairs[open_ch]
    depth, quote, i = 0, "", open_idx
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = ""
        elif c in "\"'`":
            quote = c
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return text[open_idx : i + 1]
        i += 1
    raise AssertionError(f"unbalanced {open_ch!r} block starting at line {line_of(text, open_idx)}")


def declared_block(code: str, name: str) -> str:
    """Body (``{...}`` or ``[...]``) of ``const NAME[: Type] = <block>`` in main.ts."""
    m = re.search(rf"\bconst\s+{re.escape(name)}\b[^=\n]*=\s*([{{\[])", code)
    assert m, (
        f"main.ts no longer declares `const {name} = {{...}}|[...]` (renamed or restructured?)"
    )
    return balanced_block(code, m.start(1))


# ---------------------------------------------------------------------------
# main.ts DOM lookups
# ---------------------------------------------------------------------------

# Optional TS generic argument, e.g. el<HTMLInputElement>(...) -- may nest one level.
_GENERIC = r"(?:<[^()]*?>)?"


def id_wrappers(code: str) -> list[str]:
    """Names of helpers that look an element up by id.

    ``el`` plus any ``function foo(id: string ...)`` whose first lines delegate to
    ``el(id)`` / ``document.getElementById(id)`` (``inputVal``, ``setInputVal``...).
    Auto-detected so a new wrapper is covered without touching this test.
    """
    names = {"el"}
    for m in re.finditer(r"\bfunction\s+(\w+)\s*(?:<[^()]*?>)?\(\s*id\s*:\s*string", code):
        head = code[m.end() : m.end() + 200]
        if re.search(
            r"(?<![\w.$])el\s*(?:<[^()]*?>)?\(\s*id\s*\)|getElementById\(\s*id\s*\)", head
        ):
            names.add(m.group(1))
    return sorted(names)


def literal_id_lookups(code: str) -> dict[str, list[int]]:
    """``{id: [line, ...]}`` for every LITERAL element-id lookup in (comment-free) main.ts.

    Targets, in JS/TS syntax:
      * ``el("x")`` / ``el<HTMLDivElement>("x")`` and detected wrappers (``inputVal("x")``)
      * ``document.getElementById("x")``
      * ``querySelector("#x")`` / ``querySelectorAll`` / ``closest`` / ``matches`` with
        a literal selector -- every ``#token`` inside it (template strings with
        ``${...}`` are skipped: not statically knowable).
    Dynamic ids (``el("tab-" + t)``, ``el(id)``) cannot be checked and are skipped.
    """
    found: dict[str, list[int]] = {}

    def add(i: str, idx: int) -> None:
        found.setdefault(i, []).append(line_of(code, idx))

    wrappers = "|".join(re.escape(w) for w in id_wrappers(code))
    # name<T>("literal"[,|)] -- the literal must be the WHOLE first argument
    # (not "tab-" + t), and the callee must not be a property (`.el(`).
    call = rf"(?<![\w.$])(?:{wrappers}){_GENERIC}\(\s*([\"'])([^\"'`\\$]+)\1\s*[,)]"
    for m in re.finditer(call, code):
        add(m.group(2), m.start())
    for m in re.finditer(r"getElementById\(\s*([\"'])([^\"'`\\$]+)\1\s*\)", code):
        add(m.group(2), m.start())
    sel = rf"(?:querySelector|querySelectorAll|closest|matches){_GENERIC}\(\s*([\"'`])((?:(?!\1)[^\\])*)\1"
    for m in re.finditer(sel, code):
        if "${" in m.group(2):
            continue
        for tok in re.findall(r"#([A-Za-z_][\w-]*)", m.group(2)):
            add(tok, m.start())
    return found


def ids_created_by_main_ts(code: str) -> set[str]:
    """Ids main.ts itself gives to elements it builds (so they need not be in the HTML).

    Targets: ``x.id = "a"``, ``{ id: "a" }`` (attribute bags / helper options),
    ``setAttribute("id", "a")`` and markup strings containing ``id="a"`` /
    ``id=\\"a\\"`` / ``id='a'``.
    """
    created: set[str] = set()
    patterns = (
        r"\.id\s*=\s*([\"'`])([\w-]+)\1",
        r"(?<![\w.])id\s*:\s*([\"'`])([\w-]+)\1",
        r"setAttribute\(\s*([\"'])id\1\s*,\s*([\"'`])([\w-]+)\2",
        r"\bid=\\?([\"'])([\w-]+)\\?\1",
    )
    for p in patterns:
        for m in re.finditer(p, code):
            created.add(m.groups()[-1] if p.startswith("setAttribute") else m.group(2))
    return created


# ---------------------------------------------------------------------------
# 1. every literal id main.ts looks up exists in both HTML files
# ---------------------------------------------------------------------------


def test_main_ts_lookups_exist_in_both_index_html():
    code = strip_js_comments(_read(MAIN_TS))
    lookups = literal_id_lookups(code)
    # Guard the guard: if the extraction regexes stop matching, fail loudly instead
    # of passing vacuously.
    assert len(lookups) >= 30, (
        f"only {len(lookups)} literal element ids extracted from main.ts -- the el()/"
        "getElementById extraction is probably broken by a syntax change"
    )
    created = ids_created_by_main_ts(code) | set(IDS_NOT_IN_HTML)
    problems: list[str] = []
    for label, path in (("dashboard/index.html", SRC_HTML), ("static/index.html", BUILT_HTML)):
        have = html_ids(path)
        missing = {i: ls for i, ls in lookups.items() if i not in have and i not in created}
        for i, ls in sorted(missing.items()):
            problems.append(
                f"#{i} (main.ts line {', '.join(map(str, sorted(set(ls))))}) missing from {label}"
            )
    assert not problems, (
        "main.ts looks up elements by literal id that the HTML does not declare "
        "(el() would throw and abort the whole bootstrap):\n  " + "\n  ".join(problems)
    )


def test_id_whitelist_entries_are_really_needed():
    """A stale IDS_NOT_IN_HTML entry would hide a real missing id forever."""
    code = strip_js_comments(_read(MAIN_TS))
    lookups = literal_id_lookups(code)
    stale = [i for i in IDS_NOT_IN_HTML if i not in lookups]
    assert not stale, f"IDS_NOT_IN_HTML lists ids main.ts no longer looks up: {stale}"


# ---------------------------------------------------------------------------
# 2. committed build == source; assets referenced exist; no stale assets
# ---------------------------------------------------------------------------


def _built_asset_refs() -> list[str]:
    html = _read(BUILT_HTML)
    # <script ... src="./assets/x.js"> and <link ... href="./assets/x.css">
    return re.findall(r"""(?:src|href)\s*=\s*["']([^"']*assets/[^"']+)["']""", html)


def test_built_html_declares_same_ids_as_source_html():
    src, built = html_ids(SRC_HTML), html_ids(BUILT_HTML)
    assert src and built, "an index.html declares no ids at all"
    only_src, only_built = sorted(src - built), sorted(built - src)
    assert not only_src and not only_built, (
        "static/index.html is out of sync with dashboard/index.html (rebuild the dashboard: "
        f"`npm run build`).\n  only in source: {only_src}\n  only in build:  {only_built}"
    )


def test_built_html_asset_references_exist():
    refs = _built_asset_refs()
    assert refs, "static/index.html references no ./assets/ file (no bundle?)"
    bad = [r for r in refs if not r.startswith("./assets/")]
    assert not bad, f"asset refs must be relative './assets/...' (vite base './'); got {bad}"
    missing = [r for r in refs if not (STATIC / r).is_file()]
    assert not missing, f"static/index.html references missing files: {missing}"
    assert any(r.endswith(".js") for r in refs), "static/index.html has no JS bundle reference"
    assert any(r.endswith(".css") for r in refs), "static/index.html has no CSS reference"


def test_no_stale_files_in_static_assets():
    """Vite runs with emptyOutDir; anything unreferenced is a leftover from a past build."""
    assert ASSETS.is_dir(), f"missing {ASSETS}"
    referenced = {Path(r).name for r in _built_asset_refs()}
    # Files pulled in by the bundles themselves (lazy chunks, fonts, images).
    text = "\n".join(
        _read(STATIC / r)
        for r in _built_asset_refs()
        if r.endswith((".js", ".css")) and (STATIC / r).is_file()
    )
    on_disk = sorted(p.name for p in ASSETS.iterdir() if p.is_file())
    stale = [n for n in on_disk if n not in referenced and n not in text]
    assert not stale, (
        f"stale files in static/assets (not referenced by index.html or the bundles): {stale}. "
        "Rebuild with emptyOutDir or delete them."
    )
    stale_bundles = [
        n for n in on_disk if re.fullmatch(r"index-[\w-]+\.(js|css)", n) and n not in referenced
    ]
    assert not stale_bundles, f"stale index-*.js/css bundles: {stale_bundles}"


# ---------------------------------------------------------------------------
# 3. providers
# ---------------------------------------------------------------------------


def ts_providers(code: str) -> dict[str, str]:
    """``{key: base_url}`` from main.ts's ``const PROVIDERS = { key: { base_url: "..." }, }``."""
    block = declared_block(code, "PROVIDERS")
    result: dict[str, str] = {}
    depth, quote, i = 0, "", 0
    while i < len(block):
        c = block[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = ""
        elif c in "\"'`":
            quote = c
        elif c == "{":
            if depth == 1:
                key = re.search(r"""(?:^|[\s,{])["']?([\w-]+)["']?\s*:\s*$""", block[:i])
                assert key, f"cannot read a PROVIDERS key before offset {i}"
                inner = balanced_block(block, i)
                url = re.search(r"""base_url\s*:\s*(["'`])(.*?)\1""", inner)
                assert url, f"PROVIDERS[{key.group(1)!r}] has no literal base_url"
                result[key.group(1)] = url.group(2)
                i += len(inner)
                continue
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    return result


def test_ts_providers_match_python_providers():
    ts = ts_providers(strip_js_comments(_read(MAIN_TS)))
    py = dict(llm.PROVIDERS)
    assert ts, "no providers parsed from main.ts"
    diffs = []
    for k in sorted(py.keys() - ts.keys()):
        diffs.append(f"provider {k!r} is in llm.PROVIDERS but missing from main.ts PROVIDERS")
    for k in sorted(ts.keys() - py.keys()):
        diffs.append(f"provider {k!r} is in main.ts PROVIDERS but missing from llm.PROVIDERS")
    for k in sorted(ts.keys() & py.keys()):
        if ts[k] != py[k]:
            diffs.append(f"provider {k!r} base_url differs: main.ts {ts[k]!r} vs llm.py {py[k]!r}")
    assert not diffs, "PROVIDERS drifted between dashboard and llm.py:\n  " + "\n  ".join(diffs)


def test_provider_select_options_match_providers():
    options = parse_html(SRC_HTML).options.get("provider")
    assert options, "dashboard/index.html has no <select id=provider> with options"
    assert len(options) == len(set(options)), f"duplicate <option value> in #provider: {options}"
    ts = set(ts_providers(strip_js_comments(_read(MAIN_TS))))
    only_html = sorted(set(options) - ts)
    only_ts = sorted(ts - set(options))
    assert not only_html and not only_ts, (
        "#provider <option>s and main.ts PROVIDERS disagree (picking an option without a "
        f"PROVIDERS entry breaks the base_url autofill).\n  only in HTML: {only_html}\n  only in PROVIDERS: {only_ts}"
    )


def test_built_html_provider_options_match_source():
    src = parse_html(SRC_HTML).options.get("provider")
    built = parse_html(BUILT_HTML).options.get("provider")
    assert src == built, f"#provider options differ between source {src} and build {built}"


# ---------------------------------------------------------------------------
# 4. chart types
# ---------------------------------------------------------------------------


def ts_chart_types(code: str) -> list[str]:
    block = declared_block(code, "CHART_TYPES")
    return re.findall(r"""["']([\w-]+)["']""", block)


def test_ts_chart_types_cover_server_chart_types():
    ts = ts_chart_types(strip_js_comments(_read(MAIN_TS)))
    assert ts, "no chart types parsed from main.ts CHART_TYPES"
    dups = sorted({t for t in ts if ts.count(t) > 1})
    assert not dups, f"duplicate entries in main.ts CHART_TYPES: {dups}"
    missing = [t for t in llm.CHART_TYPES if t not in ts]
    assert not missing, (
        f"the server may suggest chart types the dashboard cannot render: {missing} "
        f"(llm.CHART_TYPES={list(llm.CHART_TYPES)}, main.ts={ts})"
    )


# ---------------------------------------------------------------------------
# 5. language selector
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [SRC_HTML, BUILT_HTML], ids=["source", "built"])
def test_language_select_options(path: Path):
    options = parse_html(path).options.get("language")
    assert options, f"{path.name}: no <select id=language> with options"
    assert all(o.strip() for o in options), f"empty <option value> in #language: {options}"
    assert len(options) == len(set(options)), f"duplicate <option value> in #language: {options}"
    default = llm.LLMConfig().language
    assert default in options, (
        f"server default language {default!r} (LLMConfig().language) is not selectable in "
        f"#language {options}: the dropdown would show a language the server isn't using"
    )


# ---------------------------------------------------------------------------
# 6. hand-mirrored regex / fold function
# ---------------------------------------------------------------------------


def ts_regex_literal(code: str, name: str) -> tuple[str, str]:
    """``(source, flags)`` of ``const NAME = /source/flags;`` (JS ``/`` delimiters removed)."""
    m = re.search(
        rf"\bconst\s+{re.escape(name)}\s*=\s*/((?:\\.|\[(?:\\.|[^\]\\])*\]|[^/\\\n])+)/([a-z]*)\s*;",
        code,
    )
    assert m, f"main.ts no longer declares `const {name} = /.../;`"
    return m.group(1), m.group(2)


def test_pair_regex_mirrors_python():
    source, flags = ts_regex_literal(_read(MAIN_TS), "PAIR_RE")
    assert source == probes._PAIR_RE.pattern, (
        "main.ts PAIR_RE and probes._PAIR_RE drifted (the server and the dashboard would "
        f"disagree on what an ordered-pair category is):\n  main.ts: {source}\n  probes.py: {probes._PAIR_RE.pattern}"
    )
    assert flags == "", f"PAIR_RE gained JS flags {flags!r}; probes._PAIR_RE has none"
    assert probes._PAIR_RE.flags & ~re.UNICODE == 0, (
        "probes._PAIR_RE gained re flags; mirror them in main.ts"
    )


# Labels that exercise every step of the fold: accents (precomposed + combining),
# NFKD compatibility forms, runs/odd kinds of whitespace, case.
_TRICKY = {
    "Técnico da Seleção": "tecnico da selecao",
    "  tecnico   da\tselecao \n": "tecnico da selecao",
    "ÉCOLE": "ecole",
    "e\u0301cole": "ecole",  # already-decomposed accent
    "ﬁne": "fine",  # NFKD ligature expansion
    "Ａｌｔｏ": "alto",  # fullwidth forms
    "a\u00a0b": "a b",  # NBSP counts as whitespace
    "Ñandú": "nandu",
}


def test_python_fold_label_contract_on_tricky_labels():
    wrong = {
        k: (probes._fold_label(k), v) for k, v in _TRICKY.items() if probes._fold_label(k) != v
    }
    assert not wrong, f"probes._fold_label changed behaviour (got, expected): {wrong}"


def test_ts_fold_axis_label_still_mirrors_python_fold():
    code = strip_js_comments(_read(MAIN_TS))
    m = re.search(r"function\s+foldAxisLabel\s*\([^)]*\)[^{]*\{", code)
    assert m, "main.ts no longer defines function foldAxisLabel"
    body = balanced_block(code, m.end() - 1)
    steps = {
        "NFKD normalisation": r"""\.normalize\(\s*["']NFKD["']\s*\)""",
        "combining-mark removal": (
            r"""\.replace\(\s*/(?:\\p\{(?:Diacritic|M|Mn|Mark|Nonspacing_Mark)\}|\[\\u0300-\\u036f\]|\\p\{M[a-z]?\})/g?u?g?\s*,\s*["']{2}\s*\)"""
        ),
        "whitespace collapse": r"""\.replace\(\s*/\\s\+/g\s*,\s*["'] ["']\s*\)""",
        "lower-casing": r"""\.to(?:Locale)?LowerCase\(""",
    }
    pos: dict[str, int] = {}
    missing = []
    for name, pat in steps.items():
        hit = re.search(pat, body)
        if hit:
            pos[name] = hit.start()
        else:
            missing.append(name)
    assert not missing, (
        f"main.ts foldAxisLabel lost step(s) that probes._fold_label performs: {missing}\n"
        f"body:\n{body}"
    )
    order = list(steps)  # python order: normalize -> strip marks -> collapse ws -> lowercase
    assert [pos[k] for k in order] == sorted(pos[k] for k in order), (
        f"foldAxisLabel applies the steps in a different order than _fold_label: {sorted(pos, key=lambda step: pos[step])}"
    )
    assert re.search(r"\.trim\(\)", body) or "\\s+" in body, "foldAxisLabel no longer trims edges"


# ---------------------------------------------------------------------------
# 7. localStorage keys carry the purgeable prefix
# ---------------------------------------------------------------------------

_KEY_PREFIX = "viewlyt."
# localStorage.getItem(x) / setItem / removeItem, and the lsGet/lsSet helpers
_LS_CALL = re.compile(
    r"(?<![\w$])(?:localStorage\s*\.\s*(?:getItem|setItem|removeItem)|lsGet|lsSet)\s*\(\s*"
    r"""(?:(["'`])([^"'`]*)\1|([A-Za-z_$][\w$]*))"""
)


def local_storage_keys(code: str) -> list[tuple[str, int]]:
    """``[(key_or_prefix, line)]`` for every localStorage key used in ``code``.

    Direct literals are taken as-is (for template/concatenated keys only the static
    prefix is known and checked, which is exactly what the prefix purge cares about).
    Identifier arguments (``lsGet(THEME_KEY)``, ``localStorage.removeItem(collapsedKey)``)
    are resolved through ``const|let|var NAME = "literal" [+ ...]``. The helpers'
    own pass-through parameter (``key``) resolves to nothing and is ignored.
    """
    consts: dict[str, str] = {}
    for m in re.finditer(
        r"""\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=\s*(["'`])([^"'`]*)\2""", code
    ):
        consts[m.group(1)] = m.group(3)
    keys: list[tuple[str, int]] = []
    for m in _LS_CALL.finditer(code):
        line = line_of(code, m.start())
        if m.group(2) is not None:
            keys.append((m.group(2).split("${")[0], line))
        elif m.group(3) in consts:
            keys.append((consts[m.group(3)], line))
    return keys


def test_local_storage_keys_use_viewlyt_prefix():
    code = strip_js_comments(_read(MAIN_TS))
    keys = local_storage_keys(code)
    assert len(keys) >= 5, (
        f"only {len(keys)} localStorage keys found; extraction is probably broken"
    )
    bad = [f"{k!r} (main.ts line {ln})" for k, ln in keys if not k.startswith(_KEY_PREFIX)]
    assert not bad, (
        f"localStorage keys without the {_KEY_PREFIX!r} prefix -- the Reset button purges only "
        "that prefix, so these would survive a reset:\n  " + "\n  ".join(bad)
    )
    # And the purge itself must still be prefix-based.
    assert f'startsWith("{_KEY_PREFIX}")' in code, (
        "the Reset purge no longer filters keys by 'viewlyt.'"
    )


@pytest.mark.parametrize("path", [SRC_HTML, BUILT_HTML], ids=["source", "built"])
def test_inline_html_scripts_use_viewlyt_prefix(path: Path):
    # index.html applies the stored theme before first paint with an inline script.
    bad = [k for k, _ in local_storage_keys(_read(path)) if not k.startswith(_KEY_PREFIX)]
    assert not bad, f"{path.name}: inline script uses un-prefixed localStorage keys {bad}"


# ---------------------------------------------------------------------------
# 8. selectors the browser e2e depends on
# ---------------------------------------------------------------------------


def _e2e_ids() -> set[str]:
    ids: set[str] = set(E2E_IDS)
    if E2E_TEST.is_file():  # also follow whatever the e2e file uses today
        ids |= set(re.findall(r"""["'](?:[\w.\[\]="-]* )?#([A-Za-z_][\w-]*)""", _read(E2E_TEST)))
    return ids


def test_e2e_ids_exist_in_built_html_or_are_generated():
    created = ids_created_by_main_ts(strip_js_comments(_read(MAIN_TS)))
    have = html_ids(BUILT_HTML)
    missing = sorted(i for i in _e2e_ids() if i not in have and i not in created)
    assert not missing, (
        f"e2e selectors #{', #'.join(missing)} exist neither in static/index.html nor are created by main.ts"
    )


def _string_literals_containing_token(code: str, token: str) -> bool:
    # token as a whole class name inside a "..." / '...' / `...` literal
    pat = rf"""(["'`])[^"'`\n]*(?<![\w-]){re.escape(token)}(?![\w-])[^"'`\n]*\1"""
    return re.search(pat, code) is not None


def test_main_ts_still_builds_the_e2e_classes_and_probe_id_attribute():
    code = strip_js_comments(_read(MAIN_TS))
    # toast-success / toast-error are produced by `toast toast-${kind}`; verify the
    # template AND that showToast is really invoked with both kinds.
    toast_template = re.search(r"className\s*=\s*`[^`]*toast-\$\{(\w+)\}", code)
    missing = []
    for cls in E2E_CLASSES:
        if cls.startswith("toast-") and toast_template:
            kind = cls.removeprefix("toast-")
            used = re.search(rf"""showToast\([^;]*?,\s*["']{kind}["']""", code, re.S)
            if not used:
                missing.append(f"{cls} (template present, but no showToast(..., {kind!r}) call)")
        elif not _string_literals_containing_token(code, cls):
            missing.append(cls)
    assert not missing, (
        f"main.ts no longer creates elements with class(es): {missing} (the e2e asserts them)"
    )
    assert re.search(
        r"""dataset\[\s*["']probeId["']\s*\]\s*=|dataset\.probeId\s*=|setAttribute\(\s*["']data-probe-id["']""",
        code,
    ), (
        'main.ts no longer sets data-probe-id on the probe cards (e2e: .result-card[data-probe-id="mood"])'
    )


# ---------------------------------------------------------------------------
# 9. the built bundle is clean
# ---------------------------------------------------------------------------


def _bundles() -> list[Path]:
    files = sorted(ASSETS.glob("*.js")) + sorted(ASSETS.glob("*.css"))
    assert files, f"no .js/.css in {ASSETS}"
    return files


def test_built_bundle_has_no_dangling_sourcemap_or_dev_urls():
    problems = []
    for f in _bundles():
        text = _read(f)
        for ref in re.findall(r"sourceMappingURL=([^\s*]+)", text):
            if not (f.parent / ref).is_file():
                problems.append(f"{f.name}: sourceMappingURL={ref} points to a missing file")
        for dev in ("/src/main.ts", "@vite/client", "localhost:5173"):
            if dev in text:
                problems.append(f"{f.name}: leftover dev reference {dev!r}")
    assert not problems, "built bundle is not production-clean:\n  " + "\n  ".join(problems)


def test_built_html_is_not_the_dev_page():
    html = _read(BUILT_HTML)
    assert "/src/main.ts" not in html, (
        "static/index.html still has the dev <script src=/src/main.ts> (not built?)"
    )
    assert "@vite/client" not in html, "static/index.html references the Vite dev client"
    assert re.search(r"""<script[^>]+type=["']module["'][^>]+src=["']\./assets/""", html), (
        "static/index.html has no built module script"
    )
