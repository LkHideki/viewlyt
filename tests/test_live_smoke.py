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
