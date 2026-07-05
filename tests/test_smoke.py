"""Smoke tests: fast, browser-free sanity checks of the CLI surface and packaging.

The CLI is driven in a subprocess (conftest.cli_run) so --version / --help, which
SystemExit via argparse, and the exit codes are exercised exactly as a user sees
them. ('import viewlyt' staying selenium-free is asserted in
tests/test_units.py::test_lazy_import_no_selenium — not duplicated here.)
"""

from __future__ import annotations

import importlib.metadata as md
import os
import shutil
import subprocess
import sys

import pytest
from conftest import SRC, cli_run, cli_run_vl


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


def test_removed_entry_points_are_gone():
    """The pre-unification scripts must NOT reappear (regression guard on pyproject)."""
    names = {e.name for e in md.entry_points(group="console_scripts")}
    for dead in ("viewlyt", "viewlyt-live", "viewlyt-ask", "vlive", "vlask"):
        assert dead not in names, f"{dead} console script should have been removed"


# --------------------------------------------------------------------------- #
# Dispatcher routing (in-process, main() monkeypatched — fast and deterministic)
# --------------------------------------------------------------------------- #
def _spy(monkeypatch, module_name: str, ret: int):
    """Patch `<module>.main` with a recorder; return the dict it writes argv into."""
    import importlib

    seen: dict = {}

    def fake(argv=None):
        seen["argv"] = argv
        return ret

    monkeypatch.setattr(importlib.import_module(module_name), "main", fake)
    return seen


def test_vl_default_routes_to_scraper(monkeypatch):
    """A bare `vl ARGS` (no subcommand) dispatches to the scraper CLI verbatim."""
    from viewlyt import vl

    seen = _spy(monkeypatch, "viewlyt.cli", 0)
    assert vl.main(["https://youtu.be/x", "-c"]) == 0
    assert seen["argv"] == ["https://youtu.be/x", "-c"]


def test_vl_empty_argv_routes_to_scraper(monkeypatch):
    """`vl` with no args still goes to the scraper (its clear 'no URLs' error path)."""
    from viewlyt import vl

    seen = _spy(monkeypatch, "viewlyt.cli", 2)
    assert vl.main([]) == 2
    assert seen["argv"] == []


def test_vl_ask_subcommand_routes(monkeypatch):
    """`vl ask ARGS` dispatches to viewlyt.rag:main with the `ask` token stripped."""
    from viewlyt import vl

    seen = _spy(monkeypatch, "viewlyt.rag", 3)
    assert vl.main(["ask", "out/a.md", "q?"]) == 3
    assert seen["argv"] == ["out/a.md", "q?"]


def test_vl_live_subcommand_routes(monkeypatch):
    """`vl live ARGS` dispatches to viewlyt.live.cli:main with the `live` token stripped."""
    from viewlyt import vl

    seen = _spy(monkeypatch, "viewlyt.live.cli", 5)
    assert vl.main(["live", "https://youtu.be/live"]) == 5
    assert seen["argv"] == ["https://youtu.be/live"]


def test_vl_split_subcommand_routes(monkeypatch):
    """`vl split ARGS` dispatches to viewlyt.split:main with the `split` token stripped."""
    from viewlyt import vl

    seen = _spy(monkeypatch, "viewlyt.split", 4)
    assert vl.main(["split", "out/a.md", "out/b.md"]) == 4
    assert seen["argv"] == ["out/a.md", "out/b.md"]


def test_vl_subcommand_with_no_extra_args(monkeypatch):
    """`vl ask` alone forwards an empty argv (rag decides what to do with it)."""
    from viewlyt import vl

    seen = _spy(monkeypatch, "viewlyt.rag", 0)
    vl.main(["ask"])
    assert seen["argv"] == []


@pytest.mark.parametrize("reserved", ["ask", "live"])
def test_reserved_token_is_subcommand_only_as_first_arg(monkeypatch, reserved):
    """`vl '<url>' ask/live` is a scrape with a positional, NOT a subcommand switch."""
    from viewlyt import vl

    seen = _spy(monkeypatch, "viewlyt.cli", 0)
    vl.main(["https://youtu.be/x", reserved])
    assert seen["argv"] == ["https://youtu.be/x", reserved]


def test_vl_argv_none_reads_sys_argv(monkeypatch):
    """main(None) must fall back to sys.argv[1:] (the real console-script path)."""
    from viewlyt import vl

    seen = _spy(monkeypatch, "viewlyt.rag", 0)
    monkeypatch.setattr(sys, "argv", ["vl", "ask", "out/x.md"])
    vl.main()
    assert seen["argv"] == ["out/x.md"]


def test_vl_help_alias_routes_to_scraper_help(monkeypatch):
    """`vl help` becomes `vl --help` on the scraper."""
    from viewlyt import vl

    seen = _spy(monkeypatch, "viewlyt.cli", 0)
    vl.main(["help"])
    assert seen["argv"] == ["--help"]


@pytest.mark.parametrize(
    "mod,sub",
    [("viewlyt.rag", "ask"), ("viewlyt.live.cli", "live"), ("viewlyt.split", "split")],
)
def test_vl_help_subcommand_alias(monkeypatch, mod, sub):
    """`vl help ask` / `vl help live` becomes `<sub> --help`."""
    from viewlyt import vl

    seen = _spy(monkeypatch, mod, 0)
    vl.main(["help", sub])
    assert seen["argv"] == ["--help"]


# --------------------------------------------------------------------------- #
# End-to-end through the real dispatcher subprocess (prog names, help, lazy deps)
# --------------------------------------------------------------------------- #
def test_vl_version_via_dispatcher():
    r = cli_run_vl(["--version"])
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("vl "), r.stdout


def test_vl_help_advertises_subcommands_and_uses_new_prefix():
    r = cli_run_vl(["--help"])
    assert r.returncode == 0, r.stderr
    # scraper flags still shown (scrape is the default mode)
    for token in ("-c", "--unify", "--from-file"):
        assert token in r.stdout, token
    # subcommands are discoverable
    assert "vl ask" in r.stdout
    assert "vl live" in r.stdout
    assert "vl split" in r.stdout
    # examples use the real command, not the retired `viewlyt` prefix
    assert "vl -c" in r.stdout
    assert "viewlyt -c" not in r.stdout


@pytest.mark.parametrize(
    "argv,prefix",
    [
        (["ask", "--help"], "usage: vl ask"),
        (["live", "--help"], "usage: vl live"),
        (["split", "--help"], "usage: vl split"),
        (["help", "ask"], "usage: vl ask"),
        (["help", "live"], "usage: vl live"),
        (["help", "split"], "usage: vl split"),
    ],
)
def test_vl_subcommand_help_prog_name(argv, prefix):
    r = cli_run_vl(argv)
    assert r.returncode == 0, r.stderr
    assert prefix in r.stdout, r.stdout


@pytest.mark.parametrize("sub", ["ask", "live"])
def test_vl_subcommand_version_parity(sub):
    """Every mode answers --version with its own prog name (vl / vl ask / vl live)."""
    r = cli_run_vl([sub, "--version"])
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith(f"vl {sub} "), r.stdout


def test_vl_ask_path_never_imports_selenium_or_scraper():
    """DX/perf guarantee: the analysis path must not drag in the scraper/Selenium."""
    code = (
        "import sys\n"
        "from viewlyt.vl import main\n"
        "try:\n"
        "    main(['ask', '--help'])\n"
        "except SystemExit:\n"
        "    pass\n"
        "assert 'selenium' not in sys.modules, 'selenium imported on vl ask'\n"
        "assert 'viewlyt.cli' not in sys.modules, 'scraper CLI imported on vl ask'\n"
        "assert 'viewlyt.scraper' not in sys.modules, 'scraper imported on vl ask'\n"
        "print('OK')\n"
    )
    env = {**os.environ, "PYTHONPATH": SRC}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_installed_vl_console_script_runs():
    """The packaged `vl` binary itself resolves and answers --version (skips if not installed)."""
    exe = shutil.which("vl")
    if exe is None:
        pytest.skip("vl console script not on PATH (project not installed in this env)")
    r = subprocess.run([exe, "--version"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("vl "), r.stdout
