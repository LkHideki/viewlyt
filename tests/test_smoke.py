"""Smoke tests: fast, browser-free sanity checks of the CLI surface and packaging.

The CLI is driven in a subprocess (conftest.cli_run) so --version / --help, which
SystemExit via argparse, and the exit codes are exercised exactly as a user sees
them. ('import viewlyt' staying selenium-free is asserted in
tests/test_units.py::test_lazy_import_no_selenium — not duplicated here.)
"""

from __future__ import annotations

import importlib.metadata as md

from conftest import cli_run


def test_version_flag_smoke():
    r = cli_run(["--version"])
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("vl ")
    assert md.version("viewlyt") in r.stdout


def test_help_flag_smoke():
    r = cli_run(["--help"])
    assert r.returncode == 0, r.stderr
    for token in (
        "--comments",
        "--transcript-only",
        "--no-merge-comments",
        "--related",
        "--unify",
        "--unify-all",
        "-c",
        "-t",
    ):
        assert token in r.stdout, token


def test_no_args_exits_2_with_message():
    r = cli_run([])
    assert r.returncode == 2
    assert "no valid YouTube URLs/ids given" in r.stderr


def test_invalid_url_exits_2():
    r = cli_run(["not-a-url"])
    assert r.returncode == 2
    assert "no valid YouTube URLs/ids given" in r.stderr
    assert "ignoring" in r.stderr  # the per-item warning


def test_console_entry_point_resolves():
    eps = [e for e in md.entry_points(group="console_scripts") if e.name == "vl"]
    assert eps, "vl console script not registered"
    assert eps[0].value == "viewlyt.vl:main"
    from viewlyt.vl import main

    assert eps[0].load() is main


def test_vl_default_routes_to_scraper(monkeypatch):
    """A bare `vl ARGS` (no subcommand) dispatches to the scraper CLI verbatim."""
    import viewlyt.cli as scraper_cli
    from viewlyt import vl

    seen = {}

    def fake(argv=None):
        seen["argv"] = argv
        return 0

    monkeypatch.setattr(scraper_cli, "main", fake)
    assert vl.main(["https://youtu.be/x", "-c"]) == 0
    assert seen["argv"] == ["https://youtu.be/x", "-c"]


def test_vl_ask_subcommand_routes(monkeypatch):
    """`vl ask ARGS` dispatches to viewlyt.rag:main with the `ask` token stripped."""
    import viewlyt.rag as rag
    from viewlyt import vl

    seen = {}

    def fake(argv=None):
        seen["argv"] = argv
        return 3

    monkeypatch.setattr(rag, "main", fake)
    assert vl.main(["ask", "out/a.md", "q?"]) == 3
    assert seen["argv"] == ["out/a.md", "q?"]
