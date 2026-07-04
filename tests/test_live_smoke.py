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
    assert r.stdout.startswith("viewlyt-live ")
    assert md.version("viewlyt") in r.stdout


def test_live_help_flag_smoke():
    r = cli_run_live(["--help"])
    assert r.returncode == 0, r.stderr
    for token in ("--host", "--port", "--provider", "--base-url", "--model", "--gap", "--no-open"):
        assert token in r.stdout, token


def test_live_console_entry_point_resolves():
    eps = [e for e in md.entry_points(group="console_scripts") if e.name == "viewlyt-live"]
    assert eps, "viewlyt-live console script not registered"
    assert eps[0].value == "viewlyt.live.cli:main"
    from viewlyt.live.cli import main

    assert eps[0].load() is main
