"""Persistence round-trip: the API key is encrypted at rest and restored on load.

Uses a temp state dir (monkeypatched) so it never touches the real ``~/.viewlyt``.
Skipped when the optional ``cryptography`` dependency (``viewlyt[live]``) is absent.
"""

from __future__ import annotations

import pytest

pytest.importorskip("cryptography")

from viewlyt.live import persistence  # noqa: E402


def test_state_roundtrip_encrypts_the_key(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(persistence, "STATE_DIR", tmp_path)
    monkeypatch.setattr(persistence, "STATE_FILE", tmp_path / "live-state.json")
    monkeypatch.setattr(persistence, "KEY_FILE", tmp_path / "key")

    secret = "sk-or-v1-supersecret-123"
    persistence.save_state(
        {"n": 230, "gap": 45.0, "mode": "hybrid", "capacity": 3000},
        {"base_url": "https://openrouter.ai/api/v1", "model": "g/m", "api_key": secret},
        [{"kind": "open", "id": "o1", "label": "L", "instruction": "x"}],
    )

    raw = (tmp_path / "live-state.json").read_text(encoding="utf-8")
    assert secret not in raw  # the key is never written in the clear
    assert "api_key_enc" in raw

    st = persistence.load_state()
    assert st is not None
    assert st["model"]["api_key"] == secret  # decrypts back to the original
    assert st["window"]["n"] == 230
    assert st["probes"][0]["id"] == "o1"
    assert oct((tmp_path / "key").stat().st_mode & 0o777) == "0o600"


def test_load_missing_returns_none(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(persistence, "STATE_FILE", tmp_path / "nope.json")
    assert persistence.load_state() is None


def test_write_private_creates_file_mode_0600_atomically(tmp_path) -> None:
    """secperf S7: the helper must birth the file 0600 (no world-readable window)."""
    import stat

    p = tmp_path / "secret.bin"
    persistence._write_private(p, b"payload")
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert p.read_bytes() == b"payload"


def test_key_and_state_files_are_created_0600(tmp_path, monkeypatch) -> None:
    """secperf S7: both the Fernet key and the state file are 0600, not just the key."""
    import stat

    monkeypatch.setattr(persistence, "STATE_DIR", tmp_path)
    monkeypatch.setattr(persistence, "STATE_FILE", tmp_path / "live-state.json")
    monkeypatch.setattr(persistence, "KEY_FILE", tmp_path / "key")
    persistence._FERNETS.clear()

    persistence.save_state({"n": 1}, {"base_url": "x", "model": "m", "api_key": "sk"}, [])
    assert stat.S_IMODE((tmp_path / "key").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "live-state.json").stat().st_mode) == 0o600
