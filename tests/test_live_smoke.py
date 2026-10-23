"""Smoke tests for the viewlyt-live CLI surface and packaging.

Subprocess-driven like tests/test_smoke.py; needs no browser, network, LLM, or
even the optional 'live' extra (the parser imports no heavy deps).
"""

from __future__ import annotations

import importlib.metadata as md

from conftest import cli_run_live


def test_live_version_flag_smoke():
    r = cli_run_live(["--version"])
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("vl live ")
    assert md.version("viewlyt") in r.stdout


def test_live_help_flag_smoke():
    r = cli_run_live(["--help"])
    assert r.returncode == 0, r.stderr
    for token in (
        "--host",
        "--port",
        "--provider",
        "--base-url",
        "--model",
        "--gap",
        "--no-open",
        "--capture",
        "--decider",
    ):
        assert token in r.stdout, token


def test_vl_live_subcommand_routes(monkeypatch):
    """`vl live ARGS` dispatches to viewlyt.live.cli:main with ARGS (no `live` token)."""
    import viewlyt.live.cli as live_cli
    from viewlyt import vl

    seen = {}

    def fake_main(argv=None):
        seen["argv"] = argv
        return 7

    monkeypatch.setattr(live_cli, "main", fake_main)
    assert vl.main(["live", "--host", "0.0.0.0"]) == 7
    assert seen["argv"] == ["--host", "0.0.0.0"]


def test_decider_flag_parsing(monkeypatch):
    from viewlyt.live.cli import build_parser
    from viewlyt.live.decisions import DEFAULT_DECIDER_MODEL

    monkeypatch.delenv("LLM_DECIDER", raising=False)
    assert build_parser().parse_args(["--decider"]).decider == DEFAULT_DECIDER_MODEL
    assert build_parser().parse_args(["--decider", "x/y"]).decider == "x/y"
    assert build_parser().parse_args([]).decider == ""
    monkeypatch.setenv("LLM_DECIDER", "env/model")
    assert build_parser().parse_args([]).decider == "env/model"


def test_decider_flag_does_not_swallow_the_live_url(monkeypatch):
    """`--decider <url>`: argparse hands the URL to nargs='?', main() hands it back."""
    from viewlyt.live.cli import build_parser, untangle_decider
    from viewlyt.live.decisions import DEFAULT_DECIDER_MODEL

    monkeypatch.delenv("LLM_DECIDER", raising=False)
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    a = untangle_decider(build_parser().parse_args(["--decider", url]))
    assert (a.url, a.decider) == (url, DEFAULT_DECIDER_MODEL)
    # A real model id followed by the URL is left alone.
    a = untangle_decider(build_parser().parse_args(["--decider", "x/y", url]))
    assert (a.url, a.decider) == (url, "x/y")
    # URL first, bare flag last: nothing to untangle.
    a = untangle_decider(build_parser().parse_args([url, "--decider"]))
    assert (a.url, a.decider) == (url, DEFAULT_DECIDER_MODEL)
