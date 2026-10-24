"""Behavior tests for ``viewlyt.live.cli`` (``vl live`` entry point) and ``persistence``.

``cli.main`` is exercised end to end with ``server.run`` stubbed (no uvicorn, no network);
persistence tests always redirect ``STATE_DIR``/``STATE_FILE``/``KEY_FILE`` (and the
per-path Fernet cache) to ``tmp_path`` so the real ``~/.viewlyt`` is never touched.
"""

from __future__ import annotations

import json
import logging
import os
import runpy
import stat
import sys
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("cryptography")

from cryptography.fernet import Fernet  # noqa: E402

from viewlyt.live import cli, persistence, server  # noqa: E402
from viewlyt.live.decisions import DEFAULT_DECIDER_MODEL  # noqa: E402
from viewlyt.live.llm import PROVIDERS  # noqa: E402

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
VID = "dQw4w9WgXcQ"
_ENV = ("LLM_DECIDER", "OPENROUTER_API_KEY", "LLM_API_KEY", "LLM_NAME")


# --------------------------------------------------------------------------- cli


@pytest.fixture
def run_calls(monkeypatch):
    """Clean env (parser defaults read it at build time) + a recording ``server.run``."""
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    calls: list[dict] = []
    monkeypatch.setattr(server, "run", lambda **kw: calls.append(kw))
    return calls


def test_defaults_server_capture_forwards_everything(run_calls, capsys) -> None:
    assert cli.main([URL]) == 0
    (kw,) = run_calls
    assert kw["host"] == "127.0.0.1"
    assert kw["port"] == 8000
    assert kw["capture_url"] == URL
    assert kw["open_browser"] is True
    assert kw["allow_insecure_bind"] is False
    assert kw["decider_override"] is None
    assert kw["llm_cfg"].base_url == PROVIDERS["openrouter"]
    assert kw["llm_cfg"].model == "google/gemini-3.1-flash-lite"
    assert kw["llm_cfg"].api_key == ""
    assert kw["llm_cfg"].decider_model == ""
    w = kw["window"]
    assert (w.n, w.overlap, w.gap, w.mode, w.capacity) == (230, 0, 45.0, "hybrid", 3000)
    out = capsys.readouterr().out
    assert "vl live -> dashboard: http://127.0.0.1:8000/" in out
    assert "capture:      server-side" in out
    assert "snippet:" not in out and "chat popout" not in out


def test_flags_forwarded_to_run_and_window(run_calls, capsys) -> None:
    rc = cli.main(
        [
            URL,
            "--host", "0.0.0.0",
            "--port", "9123",
            "--allow-insecure-bind",
            "--no-open",
            "-n", "50",
            "--overlap", "5",
            "--gap", "12.5",
            "--mode", "count",
            "--capacity", "77",
            "--model", "vendor/m",
            "--api-key", "sk-flag",
        ]
    )  # fmt: skip
    assert rc == 0
    (kw,) = run_calls
    assert (kw["host"], kw["port"]) == ("0.0.0.0", 9123)
    assert kw["allow_insecure_bind"] is True
    assert kw["open_browser"] is False
    w = kw["window"]
    assert (w.n, w.overlap, w.gap, w.mode, w.capacity) == (50, 5, 12.5, "count", 77)
    assert kw["llm_cfg"].model == "vendor/m"
    assert kw["llm_cfg"].api_key == "sk-flag"
    assert "http://0.0.0.0:9123/" in capsys.readouterr().out


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_provider_resolves_base_url(run_calls, provider) -> None:
    assert cli.main([URL, "--provider", provider]) == 0
    assert run_calls[0]["llm_cfg"].base_url == PROVIDERS[provider]


def test_explicit_base_url_wins_over_provider(run_calls) -> None:
    cli.main([URL, "--provider", "openai", "--base-url", "http://gpu-box:8080/v1"])
    assert run_calls[0]["llm_cfg"].base_url == "http://gpu-box:8080/v1"


def test_base_url_equal_to_sentinel_defers_to_provider(run_calls) -> None:
    """The localhost default doubles as the "not given" sentinel, so passing it
    explicitly with another provider still resolves through the provider."""
    cli.main([URL, "--provider", "groq", "--base-url", "http://localhost:1234/v1"])
    assert run_calls[0]["llm_cfg"].base_url == PROVIDERS["groq"]


def test_api_key_and_model_env_precedence(run_calls, monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "generic")
    monkeypatch.setenv("LLM_NAME", "env/model")
    cli.main([URL])
    assert run_calls[-1]["llm_cfg"].api_key == "generic"
    assert run_calls[-1]["llm_cfg"].model == "env/model"

    monkeypatch.setenv("OPENROUTER_API_KEY", "router")  # wins over $LLM_API_KEY
    cli.main([URL])
    assert run_calls[-1]["llm_cfg"].api_key == "router"

    cli.main([URL, "--api-key", "flag", "--model", "flag/model"])  # flags win over env
    assert run_calls[-1]["llm_cfg"].api_key == "flag"
    assert run_calls[-1]["llm_cfg"].model == "flag/model"


# ------------------------------------------------------------------- capture modes


@pytest.mark.parametrize("argv", [[], [""]], ids=["no-url", "empty-string-url"])
def test_capture_server_without_url_exits_2(run_calls, capsys, argv) -> None:
    """An omitted AND an empty positional URL are both rejected (``''`` is not ``None``)."""
    with pytest.raises(SystemExit) as ei:
        cli.main(argv)
    assert ei.value.code == 2
    assert "--capture server needs the live URL" in capsys.readouterr().err
    assert run_calls == []  # fail fast: the server never boots


def test_default_capture_is_server_so_bare_decider_without_url_exits_2(run_calls, capsys) -> None:
    """Distinct path: ``--decider`` alone has no URL to give back (no untangle), and the
    default capture is ``server`` -- so the guard, not argparse, rejects it."""
    with pytest.raises(SystemExit) as ei:
        cli.main(["--decider"])
    assert ei.value.code == 2
    assert "--capture server needs the live URL" in capsys.readouterr().err
    assert run_calls == []


def test_browser_capture_prints_popout_and_snippet_urls(run_calls, capsys) -> None:
    assert cli.main([URL, "--capture", "browser", "--port", "8123"]) == 0
    out = capsys.readouterr().out
    assert f"chat popout:  https://www.youtube.com/live_chat?is_popout=1&v={VID}" in out
    assert "snippet:      http://127.0.0.1:8123/snippet.js" in out
    assert "server-side" not in out
    assert run_calls[0]["capture_url"] is None  # browser capture: the server pulls nothing


def test_browser_capture_accepts_bare_video_id(run_calls, capsys) -> None:
    cli.main([VID, "--capture", "browser"])
    assert f"v={VID}" in capsys.readouterr().out


def test_browser_capture_without_url_prints_snippet_only(run_calls, capsys) -> None:
    assert cli.main(["--capture", "browser"]) == 0
    out = capsys.readouterr().out
    assert "chat popout" not in out
    assert "snippet:      http://127.0.0.1:8000/snippet.js" in out
    assert run_calls[0]["capture_url"] is None


def test_popout_banner_failure_is_swallowed(run_calls, capsys, monkeypatch) -> None:
    import viewlyt.scraper as scraper

    def boom(_value: str) -> str:
        raise ValueError("unparseable")

    monkeypatch.setattr(scraper, "extract_video_id", boom)
    assert cli.main(["whatever", "--capture", "browser"]) == 0
    out = capsys.readouterr().out
    assert "chat popout" not in out
    assert "snippet:" in out  # the banner degrades, the launch continues
    assert len(run_calls) == 1


# ---------------------------------------------------------------------- decider


def test_decider_not_given_is_none_so_saved_choice_is_kept(run_calls) -> None:
    cli.main([URL])
    assert run_calls[0]["decider_override"] is None
    assert run_calls[0]["llm_cfg"].decider_model == ""


def test_decider_flag_alone_uses_default_model(run_calls) -> None:
    cli.main([URL, "--decider"])
    assert run_calls[0]["decider_override"] == DEFAULT_DECIDER_MODEL
    assert run_calls[0]["llm_cfg"].decider_model == DEFAULT_DECIDER_MODEL


def test_decider_flag_with_model(run_calls) -> None:
    cli.main([URL, "--decider", "vendor/tiny"])
    assert run_calls[0]["decider_override"] == "vendor/tiny"
    assert run_calls[0]["llm_cfg"].decider_model == "vendor/tiny"


def test_no_decider_is_explicit_empty_string_not_none(run_calls) -> None:
    cli.main([URL, "--no-decider"])
    assert run_calls[0]["decider_override"] == ""
    assert run_calls[0]["llm_cfg"].decider_model == ""


def test_decider_env_is_used_and_overridden_by_flags(run_calls, monkeypatch) -> None:
    monkeypatch.setenv("LLM_DECIDER", "env/decider")
    cli.main([URL])
    assert run_calls[-1]["decider_override"] == "env/decider"
    assert run_calls[-1]["llm_cfg"].decider_model == "env/decider"

    cli.main([URL, "--no-decider"])  # explicit off beats the env
    assert run_calls[-1]["decider_override"] == ""

    cli.main([URL, "--decider", "flag/decider"])  # explicit model beats the env
    assert run_calls[-1]["decider_override"] == "flag/decider"

    monkeypatch.setenv("LLM_DECIDER", "")  # empty env == not given
    cli.main([URL])
    assert run_calls[-1]["decider_override"] is None


def test_decider_swallowing_the_url_is_untangled(run_calls) -> None:
    """``--decider <url>``: argparse hands the URL to --decider; main() gives it back."""
    cli.main(["--decider", URL])
    (kw,) = run_calls
    assert kw["capture_url"] == URL
    assert kw["decider_override"] == DEFAULT_DECIDER_MODEL
    assert kw["llm_cfg"].decider_model == DEFAULT_DECIDER_MODEL


def test_decider_swallowing_a_bare_video_id_is_untangled(run_calls) -> None:
    cli.main(["--decider", VID])
    assert run_calls[0]["capture_url"] == VID
    assert run_calls[0]["decider_override"] == DEFAULT_DECIDER_MODEL


def test_decider_model_followed_by_url_is_not_untangled(run_calls) -> None:
    cli.main(["--decider", "vendor/tiny", URL])
    assert run_calls[0]["capture_url"] == URL
    assert run_calls[0]["decider_override"] == "vendor/tiny"


def test_untangle_leaves_decider_alone_when_url_already_given() -> None:
    args = cli.build_parser().parse_args([URL, "--decider", VID])
    out = cli.untangle_decider(args)
    assert out.url == URL
    assert out.decider == VID  # a url exists, so the value is the model


@pytest.mark.parametrize("decider", [None, ""])
def test_untangle_ignores_falsy_decider(decider) -> None:
    args = cli.build_parser().parse_args([])
    args.decider = decider
    out = cli.untangle_decider(args)
    assert out.url is None
    assert out.decider == decider


def test_untangle_returns_same_namespace_and_rewrites_it() -> None:
    args = cli.build_parser().parse_args(["--decider", "youtu.be/dQw4w9WgXcQ"])
    assert args.url is None
    assert cli.untangle_decider(args) is args
    assert args.url == "youtu.be/dQw4w9WgXcQ"
    assert args.decider == DEFAULT_DECIDER_MODEL


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://youtu.be/dQw4w9WgXcQ", True),
        ("ftp://anything", True),  # any scheme
        ("YouTube.com/live/abc", True),  # case-insensitive host
        ("www.youtu.be/x", True),
        ("dQw4w9WgXcQ", True),  # bare 11-char id
        ("abcdefghijk", True),
        ("openrouter/some-model", False),  # provider prefix
        ("abc/defghijk", False),  # 11 chars but contains "/"
        ("pplx-decider-flash-v1.2", False),  # longer than 11, has "."
        ("gpt-4o", False),  # shorter than 11
        ("abcdefghijkl", False),  # 12 chars
        ("abcdefghij!", False),  # 11 chars with illegal char
    ],
)
def test_looks_like_live_url(value, expected) -> None:
    assert cli._looks_like_live_url(value) is expected


# ------------------------------------------------------------- lifecycle / version


def test_keyboard_interrupt_from_server_exits_0(monkeypatch, capsys) -> None:
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)

    def interrupted(**_kw) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(server, "run", interrupted)
    assert cli.main([URL]) == 0
    assert "dashboard:" in capsys.readouterr().out


def test_other_server_errors_propagate(monkeypatch) -> None:
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)

    def broken(**_kw) -> None:
        raise RuntimeError("port in use")

    monkeypatch.setattr(server, "run", broken)
    with pytest.raises(RuntimeError, match="port in use"):
        cli.main([URL])


def test_version_flag_prints_installed_version(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "version", lambda _name: "9.8.7")
    with pytest.raises(SystemExit) as ei:
        cli.build_parser().parse_args(["--version"])
    assert ei.value.code == 0
    assert capsys.readouterr().out.strip() == "vl live 9.8.7"


def test_version_falls_back_when_package_not_installed(monkeypatch, capsys) -> None:
    def missing(_name: str) -> str:
        raise PackageNotFoundError

    monkeypatch.setattr(cli, "version", missing)
    with pytest.raises(SystemExit) as ei:
        cli.build_parser().parse_args(["-V"])
    assert ei.value.code == 0
    assert capsys.readouterr().out.strip() == "vl live 0.0.0"


def test_module_main_guard_exits_with_main_return_code(monkeypatch, run_calls) -> None:
    monkeypatch.setattr(sys, "argv", ["vl-live", "--capture", "browser", "--no-open"])
    # runpy re-executes the module as __main__; monkeypatch restores the real entry on teardown.
    monkeypatch.delitem(sys.modules, "viewlyt.live.cli")
    with pytest.raises(SystemExit) as ei:
        runpy.run_module("viewlyt.live.cli", run_name="__main__")
    assert ei.value.code == 0
    assert run_calls[0]["open_browser"] is False


# ------------------------------------------------------------------ persistence


@pytest.fixture
def state(tmp_path, monkeypatch):
    """Redirect every persistence path to ``tmp_path`` and start with a cold Fernet cache."""
    d = tmp_path / "viewlyt-home"
    monkeypatch.setattr(persistence, "STATE_DIR", d)
    monkeypatch.setattr(persistence, "STATE_FILE", d / "live-state.json")
    monkeypatch.setattr(persistence, "KEY_FILE", d / "key")
    monkeypatch.setattr(persistence, "_FERNETS", {})
    return d


WINDOW = {"n": 230, "gap": 45.0, "mode": "hybrid", "capacity": 3000}
MODEL = {"base_url": "https://x/v1", "model": "g/m", "api_key": "sk-secret"}


def _mode(p) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_roundtrip_all_model_fields_and_probes(state) -> None:
    probes = [{"kind": "open", "id": "o1", "label": "L", "instruction": "x"}]
    model = {
        **MODEL,
        "budget": 2,  # int is coerced to float
        "language": "English",
        "decider_model": "vendor/tiny",
    }
    persistence.save_state(WINDOW, model, probes)
    st = persistence.load_state()
    assert st == {
        "window": WINDOW,
        "model": {
            "base_url": "https://x/v1",
            "model": "g/m",
            "budget": 2.0,
            "language": "English",
            "decider_model": "vendor/tiny",
            "api_key": "sk-secret",
        },
        "probes": probes,
    }
    assert isinstance(st["model"]["budget"], float)


def test_save_applies_defaults_for_missing_or_falsy_model_fields(state) -> None:
    persistence.save_state(
        WINDOW, {"base_url": "u", "model": "m", "language": "", "api_key": None}, []
    )
    # The SAVE side must apply the defaults itself: inspect the raw JSON, since load_state
    # re-applies the same defaults and would mask a save-side regression.
    raw = json.loads(persistence.STATE_FILE.read_text(encoding="utf-8"))
    assert raw["model"]["budget"] == 0.0
    assert raw["model"]["language"] == "Portuguese (Brazil)"
    assert raw["model"]["decider_model"] == ""
    enc = raw["model"]["api_key_enc"].encode()
    assert Fernet(persistence.KEY_FILE.read_bytes()).decrypt(enc) == b""
    assert raw["probes"] == []
    st = persistence.load_state()
    assert st["model"]["budget"] == 0.0
    assert st["model"]["language"] == "Portuguese (Brazil)"
    assert st["model"]["decider_model"] == ""
    assert st["model"]["api_key"] == ""
    assert st["probes"] == []


def test_load_applies_defaults_for_fields_missing_from_an_older_file(state) -> None:
    """A state file written before budget/language/decider/probes existed still loads."""
    f = persistence._fernet()  # creates dir + key
    payload = {
        "window": {"n": 10},
        "model": {"base_url": "u", "model": "m", "api_key_enc": f.encrypt(b"k").decode()},
    }
    persistence.STATE_FILE.write_text(json.dumps(payload), encoding="utf-8")
    st = persistence.load_state()
    assert st == {
        "window": {"n": 10},
        "model": {
            "base_url": "u",
            "model": "m",
            "budget": 0.0,
            "language": "Portuguese (Brazil)",
            "decider_model": "",
            "api_key": "k",
        },
        "probes": [],
    }


def test_api_key_never_written_in_clear(state) -> None:
    persistence.save_state(WINDOW, MODEL, [])
    raw = persistence.STATE_FILE.read_text(encoding="utf-8")
    assert "sk-secret" not in raw
    assert json.loads(raw)["model"]["api_key_enc"]
    assert "api_key" not in json.loads(raw)["model"]


def test_load_missing_file_returns_none_without_creating_key(state, caplog) -> None:
    with caplog.at_level(logging.DEBUG, logger="viewlyt.live"):
        assert persistence.load_state() is None
    assert not persistence.KEY_FILE.exists()  # a pure read does not touch disk
    assert not state.exists()
    # A first run (no file yet) is normal: it must not log a warning/traceback.
    assert caplog.records == []


def test_corrupt_state_file_returns_none_and_warns(state, caplog) -> None:
    persistence.save_state(WINDOW, MODEL, [])
    persistence.STATE_FILE.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="viewlyt.live"):
        assert persistence.load_state() is None
    assert any("could not load live state" in r.message for r in caplog.records)


def test_non_utf8_state_file_returns_none(state) -> None:
    persistence.save_state(WINDOW, MODEL, [])
    persistence.STATE_FILE.write_bytes(b"\xff\xfe\x00garbage")
    assert persistence.load_state() is None


@pytest.mark.parametrize(
    "payload",
    [
        {"window": {}, "probes": []},  # no "model"
        {"model": {"base_url": "u", "model": "m"}, "window": {}},  # no api_key_enc
        {"window": {}, "model": {"api_key_enc": "garbage"}},  # undecryptable token
        [1, 2, 3],  # wrong top-level type
    ],
)
def test_bad_payload_shapes_return_none(state, payload) -> None:
    persistence._fernet()
    persistence.STATE_FILE.write_text(json.dumps(payload), encoding="utf-8")
    assert persistence.load_state() is None


def test_missing_window_key_returns_none(state) -> None:
    f = persistence._fernet()
    payload = {"model": {"base_url": "u", "model": "m", "api_key_enc": f.encrypt(b"k").decode()}}
    persistence.STATE_FILE.write_text(json.dumps(payload), encoding="utf-8")
    assert persistence.load_state() is None


def test_wrong_fernet_key_returns_none_and_does_not_leak_plaintext(state, caplog) -> None:
    persistence.save_state(WINDOW, MODEL, [])
    # Key rotated/replaced behind the app's back (and the cache reset, as on restart).
    persistence.KEY_FILE.write_bytes(Fernet.generate_key())
    persistence._FERNETS.clear()
    with caplog.at_level(logging.WARNING, logger="viewlyt.live"):
        assert persistence.load_state() is None
    assert any("could not load live state" in r.message for r in caplog.records)
    assert "sk-secret" not in caplog.text


def test_save_after_key_loss_overwrites_with_new_key_and_loads(state) -> None:
    persistence.save_state(WINDOW, MODEL, [])
    persistence.KEY_FILE.write_bytes(Fernet.generate_key())
    persistence._FERNETS.clear()
    assert persistence.load_state() is None
    persistence.save_state(WINDOW, MODEL, [])  # recovers: re-encrypts under the new key
    assert persistence.load_state()["model"]["api_key"] == "sk-secret"


def test_save_failure_is_logged_not_raised(state, monkeypatch, caplog) -> None:
    def disk_full(_path, _data) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(persistence, "_write_private", disk_full)
    with caplog.at_level(logging.WARNING, logger="viewlyt.live"):
        persistence.save_state(WINDOW, MODEL, [])  # must not raise
    assert any("could not persist live state" in r.message for r in caplog.records)
    assert not persistence.STATE_FILE.exists()


def test_save_failure_when_state_dir_is_not_creatable(state, monkeypatch, caplog) -> None:
    blocker = state.parent / "blocker"
    blocker.write_text("i am a file")
    monkeypatch.setattr(persistence, "STATE_DIR", blocker)  # mkdir over a file -> error
    monkeypatch.setattr(persistence, "STATE_FILE", blocker / "live-state.json")
    monkeypatch.setattr(persistence, "KEY_FILE", blocker / "key")
    with caplog.at_level(logging.WARNING, logger="viewlyt.live"):
        persistence.save_state(WINDOW, MODEL, [])
    (rec,) = [r for r in caplog.records if "could not persist live state" in r.message]
    assert rec.exc_info is not None  # the cause is logged, not just the message
    assert blocker.read_text() == "i am a file"  # the obstacle was left untouched


@pytest.mark.parametrize("bad_model", [{}, {"base_url": "u"}, {"model": "m"}])
def test_save_with_incomplete_model_is_swallowed_and_writes_nothing(state, bad_model) -> None:
    persistence.save_state(WINDOW, bad_model, [])
    assert not persistence.STATE_FILE.exists()


def test_unserializable_probes_do_not_clobber_existing_state(state) -> None:
    persistence.save_state(WINDOW, MODEL, [{"id": "ok"}])
    persistence.save_state(WINDOW, MODEL, [{"id": object()}])  # json.dumps raises
    assert persistence.load_state()["probes"] == [{"id": "ok"}]


def test_state_and_key_files_are_0600_and_dir_0700(state) -> None:
    persistence.save_state(WINDOW, MODEL, [])
    assert _mode(persistence.STATE_FILE) == 0o600
    assert _mode(persistence.KEY_FILE) == 0o600
    assert _mode(state) == 0o700


@pytest.fixture
def no_chmod(monkeypatch):
    """Neutralise every ``Path.chmod`` and the umask, so only the *creation* mode counts.

    The trailing ``chmod`` in ``_write_private`` masks a wrong ``os.open`` mode in a
    final-mode check; with it disabled, a 0600 file proves the TOCTOU-free creation.
    """
    monkeypatch.setattr(Path, "chmod", lambda *_a, **_k: None)
    old = os.umask(0)
    yield
    os.umask(old)


def test_write_private_creates_file_0600_atomically(tmp_path, no_chmod, monkeypatch) -> None:
    modes: list[int] = []
    real_open = os.open

    def spy(path, flags, mode=0o777, **kw):
        modes.append(mode)
        return real_open(path, flags, mode, **kw)

    monkeypatch.setattr(persistence.os, "open", spy)
    p = tmp_path / "secret"
    persistence._write_private(p, b"data")
    assert modes == [0o600]  # requested at creation, before any byte is written
    assert p.read_bytes() == b"data"
    assert _mode(p) == 0o600  # umask 0 + chmod disabled: purely the creation mode


def test_save_state_creates_state_and_key_0600_without_chmod(state, no_chmod) -> None:
    persistence.save_state(WINDOW, MODEL, [])
    assert persistence.load_state()["model"]["api_key"] == "sk-secret"
    assert _mode(persistence.STATE_FILE) == 0o600
    assert _mode(persistence.KEY_FILE) == 0o600


def test_legacy_loose_permissions_are_tightened(state) -> None:
    state.mkdir(mode=0o755)
    state.chmod(0o755)
    persistence.STATE_FILE.write_text("old", encoding="utf-8")
    persistence.STATE_FILE.chmod(0o644)
    persistence.save_state(WINDOW, MODEL, [])
    assert _mode(state) == 0o700
    assert _mode(persistence.STATE_FILE) == 0o600


def test_write_private_truncates_and_tightens_existing_file(tmp_path) -> None:
    p = tmp_path / "f"
    p.write_bytes(b"x" * 100)
    p.chmod(0o644)
    persistence._write_private(p, b"short")
    assert p.read_bytes() == b"short"  # O_TRUNC: no stale tail
    assert _mode(p) == 0o600


def test_key_is_generated_once_and_reused_across_saves(state) -> None:
    persistence.save_state(WINDOW, MODEL, [])
    key1 = persistence.KEY_FILE.read_bytes()
    persistence.save_state(WINDOW, {**MODEL, "api_key": "other"}, [])
    assert persistence.KEY_FILE.read_bytes() == key1
    Fernet(key1)  # a valid Fernet key
    assert persistence.load_state()["model"]["api_key"] == "other"


def test_existing_key_file_is_reused_not_regenerated(state) -> None:
    state.mkdir(mode=0o700)
    key = Fernet.generate_key()
    persistence.KEY_FILE.write_bytes(key)
    persistence.save_state(WINDOW, MODEL, [])
    assert persistence.KEY_FILE.read_bytes() == key
    enc = json.loads(persistence.STATE_FILE.read_text())["model"]["api_key_enc"]
    assert Fernet(key).decrypt(enc.encode()) == b"sk-secret"


def test_fernet_is_cached_per_key_path(state, tmp_path, monkeypatch) -> None:
    f1 = persistence._fernet()
    persistence.KEY_FILE.unlink()  # a cache hit must not re-read (or regenerate) the key
    assert persistence._fernet() is f1
    assert not persistence.KEY_FILE.exists()

    other = tmp_path / "other-key"  # a different path gets its own Fernet
    monkeypatch.setattr(persistence, "KEY_FILE", other)
    f2 = persistence._fernet()
    assert f2 is not f1
    assert other.exists()
    assert persistence._fernet() is f2
    monkeypatch.setattr(persistence, "KEY_FILE", state / "key")
    assert persistence._fernet() is f1  # the first path still resolves to its own Fernet
