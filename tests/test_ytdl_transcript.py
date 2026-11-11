"""Browserless transcript: yt-dlp captions first, Selenium panel as the fallback.

Three layers, none touching the network or a browser:

* pure helpers in ``viewlyt.ytdl`` (``_json3_to_segments``, ``_pick_caption_track``);
* ``video_transcript`` / ``_extract_with_captions`` with the I/O boundary patched
  (the latter against a stand-in ``yt_dlp`` module, never yt-dlp's internals);
* ``cli.main`` / ``cli.run_batch`` orchestration with ``ytdl.video_transcript``,
  ``cli.scrape_one`` and ``cli.build_primed_driver`` replaced.

The shared autouse fixture in conftest makes ``ytdl.video_transcript`` raise for
every test; the rig below overrides it explicitly where a test wants yt-dlp to answer.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import types

import pytest
from conftest import SRC, FakeDriver
from conftest import make_comment as _c

import viewlyt.cli as cli
from viewlyt import ytdl
from viewlyt.htmltext import slugify
from viewlyt.scraper import BlockedError

# Captured at import (collection) time, BEFORE the autouse fixture replaces the
# module attribute with a raising stub for each test.
real_video_transcript = ytdl.video_transcript

VID = "dQw4w9WgXcQ"


# --------------------------------------------------------------------------- #
# _json3_to_segments (pure)
# --------------------------------------------------------------------------- #
def test_json3_manual_track_collapses_embedded_newlines():
    data = {
        "events": [
            {
                "tStartMs": 180,
                "segs": [{"utf8": "- [Derek] On the morning\nof the 17th of April, 2013,"}],
            },
            {"tStartMs": 4200, "dDurationMs": 2000, "segs": [{"utf8": "  a   second\tcaption "}]},
        ]
    }
    assert ytdl._json3_to_segments(data) == [
        ("0:00", "- [Derek] On the morning of the 17th of April, 2013,"),
        ("0:04", "a second caption"),
    ]


def test_json3_asr_track_skips_aappend_newline_events():
    data = {
        "events": [
            {
                "tStartMs": 1630,
                "dDurationMs": 4000,
                "segs": [{"utf8": "hello"}, {"utf8": " world"}],
            },
            {"tStartMs": 1630, "aAppend": 1, "segs": [{"utf8": "\n"}]},
            {"tStartMs": 5000, "segs": [{"utf8": "next"}, {"utf8": " line", "tOffsetMs": 240}]},
            {"tStartMs": 5000, "aAppend": 1, "segs": [{"utf8": "\n"}]},
        ]
    }
    assert ytdl._json3_to_segments(data) == [("0:01", "hello world"), ("0:05", "next line")]


def test_json3_aappend_event_is_skipped_even_with_real_text():
    # aAppend marks a layout continuation; its utf8 is never a new caption.
    data = {"events": [{"tStartMs": 0, "aAppend": 1, "segs": [{"utf8": "dup"}]}]}
    assert ytdl._json3_to_segments(data) == []


def test_json3_drops_events_without_segs_and_empty_text():
    data = {
        "events": [
            {"tStartMs": 0, "wWinId": 1},  # window housekeeping, no segs
            {"tStartMs": 100, "segs": []},
            {"tStartMs": 200, "segs": [{"utf8": "\n"}]},  # empty once collapsed
            {"tStartMs": 300, "segs": [{"utf8": "   "}, {"utf8": None}, {}]},
            {"tStartMs": 400, "segs": [{"utf8": "[Music]"}]},  # markers survive
        ]
    }
    assert ytdl._json3_to_segments(data) == [("0:00", "[Music]")]


def test_json3_never_deduplicates_repeated_text():
    data = {
        "events": [
            {"tStartMs": 1000, "segs": [{"utf8": "la la"}]},
            {"tStartMs": 3000, "segs": [{"utf8": "la la"}]},
        ]
    }
    assert [t for _, t in ytdl._json3_to_segments(data)] == ["la la", "la la"]


@pytest.mark.parametrize(
    ("ms", "stamp"),
    [
        (0, "0:00"),
        (999, "0:00"),  # floors to the second
        (61_000, "1:01"),
        (3_599_999, "59:59"),
        (3_600_000, "1:00:00"),
        (3_723_000, "1:02:03"),
        (36_000_000, "10:00:00"),
    ],
)
def test_json3_timestamp_format_incl_hours(ms, stamp):
    assert ytdl._json3_to_segments({"events": [{"tStartMs": ms, "segs": [{"utf8": "x"}]}]}) == [
        (stamp, "x")
    ]


def test_json3_missing_start_yields_empty_stamp():
    assert ytdl._json3_to_segments({"events": [{"segs": [{"utf8": "x"}]}]}) == [("", "x")]


@pytest.mark.parametrize(
    "garbage",
    [
        None,
        [],
        "not json",
        42,
        {},
        {"events": None},
        {"events": "abc"},
        {"events": {"tStartMs": 0}},
        {"events": [None, 1, "s", [], {"segs": None}, {"segs": "abc"}, {"segs": [None, 3]}]},
    ],
)
def test_json3_garbage_input_is_empty_not_an_error(garbage):
    assert ytdl._json3_to_segments(garbage) == []


@pytest.mark.parametrize("bad", [5, 5.5, True, None, [], {}, ["a"], {"x": "y"}])
def test_json3_non_string_utf8_is_ignored_not_a_crash(bad):
    # `"".join` over a non-str used to raise TypeError: "never raises" was not true.
    assert ytdl._json3_to_segments({"events": [{"tStartMs": 0, "segs": [{"utf8": bad}]}]}) == []
    mixed = {"events": [{"tStartMs": 1000, "segs": [{"utf8": bad}, {"utf8": "ok"}, {"utf8": bad}]}]}
    assert ytdl._json3_to_segments(mixed) == [("0:01", "ok")]


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"])
def test_json3_non_finite_start_gives_an_empty_stamp_not_an_error(literal):
    # json.loads happily parses these into floats; int(nan) / int(inf) used to raise.
    data = json.loads(f'{{"events": [{{"tStartMs": {literal}, "segs": [{{"utf8": "x"}}]}}]}}')
    assert ytdl._json3_to_segments(data) == [("", "x")]


@pytest.mark.parametrize("start", [None, "5", [], {}, True, False, -1, -1500.5])
def test_json3_other_unusable_starts_leave_the_stamp_empty(start):
    data = {"events": [{"tStartMs": start, "segs": [{"utf8": "x"}]}]}
    assert ytdl._json3_to_segments(data) == [("", "x")]


def test_json3_float_and_huge_starts_still_format():
    seg = [{"utf8": "x"}]
    assert ytdl._json3_to_segments({"events": [{"tStartMs": 1500.7, "segs": seg}]}) == [
        ("0:01", "x")
    ]
    out = ytdl._json3_to_segments({"events": [{"tStartMs": 10**30, "segs": seg}]})
    assert len(out) == 1 and out[0][0] != "" and out[0][1] == "x"


def test_json3_one_bad_event_does_not_spoil_its_neighbours():
    data = {
        "events": [
            {"tStartMs": 0, "segs": [{"utf8": "a"}]},
            {"tStartMs": float("nan"), "segs": [{"utf8": 7}, {"utf8": "b"}]},
            {"tStartMs": 4000, "segs": [{"utf8": "c"}]},
        ]
    }
    assert ytdl._json3_to_segments(data) == [("0:00", "a"), ("", "b"), ("0:04", "c")]


def test_json3_output_feeds_format_transcript_unchanged():
    from viewlyt.htmltext import format_transcript

    segs = ytdl._json3_to_segments(
        {"events": [{"tStartMs": 2000, "segs": [{"utf8": "hi\nthere"}]}]}
    )
    assert format_transcript(segs) == ["[0:02] hi there"]


# --------------------------------------------------------------------------- #
# _pick_caption_track (pure)
# --------------------------------------------------------------------------- #
def _tracks(url: str) -> list[dict]:
    """A yt-dlp track list: several formats, json3 among them (not first)."""
    return [
        {"ext": "vtt", "url": f"{url}&fmt=vtt"},
        {"ext": "json3", "url": url},
        {"ext": "srv1", "url": f"{url}&fmt=srv1"},
    ]


def test_pick_prefers_manual_track_in_video_language():
    info = {
        "language": "en",
        "subtitles": {"en": _tracks("manual-en"), "fr": _tracks("manual-fr")},
        "automatic_captions": {"en-orig": _tracks("asr-en-orig"), "en": _tracks("asr-en")},
    }
    assert ytdl._pick_caption_track(info) == "manual-en"


def test_pick_then_original_asr_over_plain_asr():
    info = {
        "language": "en",
        "subtitles": {"fr": _tracks("manual-fr")},
        "automatic_captions": {"en": _tracks("asr-en"), "en-orig": _tracks("asr-en-orig")},
    }
    assert ytdl._pick_caption_track(info) == "asr-en-orig"


def test_pick_then_plain_asr_for_the_language():
    info = {"language": "en", "automatic_captions": {"en": _tracks("asr-en"), "fr": _tracks("x")}}
    assert ytdl._pick_caption_track(info) == "asr-en"


def test_pick_missing_language_takes_the_asr_original_over_a_manual_track_in_another_language():
    # REWRITTEN (was `..._first_orig_key_when_language_is_missing`, commented "the ASR original
    # beats a manual track"). That held for ANY manual track, which let ASR beat a creator's
    # captions; now it only holds when the manual track is in a DIFFERENT language: the
    # spoken language is read off the `-orig` key (pt), and a manual `fr` is not pt.
    info = {
        "language": None,
        "subtitles": {"fr": _tracks("manual-fr")},
        "automatic_captions": {"de": _tracks("asr-de"), "pt-orig": _tracks("asr-pt-orig")},
    }
    assert ytdl._pick_caption_track(info) == "asr-pt-orig"


def test_pick_regional_manual_track_beats_the_asr_original():
    # language "pt", the creator filed the track under "pt-BR": a human track in the
    # video's language must win over the ASR, not lose to it for want of an exact key.
    info = {
        "language": "pt",
        "subtitles": {"pt-BR": _tracks("manual-pt-BR")},
        "automatic_captions": {"pt-orig": _tracks("asr-pt-orig"), "pt": _tracks("asr-pt")},
    }
    assert ytdl._pick_caption_track(info) == "manual-pt-BR"


def test_pick_missing_language_prefers_a_manual_track_in_the_asr_language():
    info = {
        "language": None,
        "subtitles": {"en": _tracks("manual-en")},
        "automatic_captions": {"en-orig": _tracks("asr-en-orig"), "en": _tracks("asr-en")},
    }
    assert ytdl._pick_caption_track(info) == "manual-en"


def test_pick_missing_language_matches_a_regional_manual_track_to_the_orig_language():
    info = {
        "subtitles": {"pt-BR": _tracks("manual-pt-BR"), "fr": _tracks("manual-fr")},
        "automatic_captions": {"pt-orig": _tracks("asr-pt-orig")},
    }
    assert ytdl._pick_caption_track(info) == "manual-pt-BR"


def test_pick_exact_language_key_beats_a_regional_variant():
    both = {"pt": _tracks("manual-pt"), "pt-BR": _tracks("manual-pt-BR")}
    assert ytdl._pick_caption_track({"language": "pt", "subtitles": both}) == "manual-pt"
    assert ytdl._pick_caption_track({"language": "pt-BR", "subtitles": both}) == "manual-pt-BR"


def test_pick_language_with_a_region_finds_the_plain_manual_track():
    info = {"language": "pt-BR", "subtitles": {"pt": _tracks("manual-pt")}}
    assert ytdl._pick_caption_track(info) == "manual-pt"


def test_pick_regional_variants_are_chosen_deterministically():
    pt_pt, pt_br = _tracks("manual-pt-PT"), _tracks("manual-pt-BR")
    forward = {"language": "pt", "subtitles": {"pt-PT": pt_pt, "pt-BR": pt_br}}
    backward = {"language": "pt", "subtitles": {"pt-BR": pt_br, "pt-PT": pt_pt}}
    # Sorted key order, whatever the dict's insertion order: pt-BR < pt-PT.
    assert ytdl._pick_caption_track(forward) == "manual-pt-BR"
    assert ytdl._pick_caption_track(backward) == "manual-pt-BR"


def test_pick_asr_original_matches_by_primary_subtag_too():
    info = {"language": "pt-BR", "automatic_captions": {"pt-orig": _tracks("asr-pt-orig")}}
    assert ytdl._pick_caption_track(info) == "asr-pt-orig"


def test_pick_a_regional_translated_asr_track_is_never_taken():
    # `pt-BR` among the ASR keys is a machine TRANSLATION into Brazilian Portuguese of
    # whatever the video was spoken in: matching it by primary subtag would break the
    # "never a translated ASR track" rule.
    info = {
        "language": "pt",
        "automatic_captions": {"pt-BR": _tracks("t-pt-BR"), "en": _tracks("t-en")},
    }
    assert ytdl._pick_caption_track(info) is None


def test_pick_a_manual_track_in_another_language_does_not_beat_the_asr_original():
    info = {
        "language": "pt",
        "subtitles": {"fr": _tracks("manual-fr")},
        "automatic_captions": {"pt-orig": _tracks("asr-pt-orig")},
    }
    assert ytdl._pick_caption_track(info) == "asr-pt-orig"


def test_pick_a_regional_variant_without_json3_falls_through_to_the_next_variant():
    info = {
        "language": "pt",
        "subtitles": {
            "pt-BR": [{"ext": "vtt", "url": "manual-pt-BR.vtt"}],
            "pt-PT": _tracks("manual-pt-PT"),
        },
    }
    assert ytdl._pick_caption_track(info) == "manual-pt-PT"


def test_pick_live_chat_is_never_matched_as_a_language_variant():
    info = {"language": "live", "subtitles": {"live_chat": _tracks("chat")}}
    assert ytdl._pick_caption_track(info) is None


@pytest.mark.parametrize(
    ("tag", "primary"),
    [("pt-BR", "pt"), ("pt_BR", "pt"), ("ZH-Hans", "zh"), ("en", "en"), ("EN-orig", "en")],
)
def test_primary_subtag(tag, primary):
    assert ytdl._primary(tag) == primary


def test_pick_first_orig_key_when_language_matches_nothing():
    info = {"language": "en", "automatic_captions": {"es-orig": _tracks("asr-es-orig")}}
    assert ytdl._pick_caption_track(info) == "asr-es-orig"


def test_pick_then_first_manual_track():
    info = {"subtitles": {"fr": _tracks("manual-fr"), "de": _tracks("manual-de")}}
    assert ytdl._pick_caption_track(info) == "manual-fr"


def test_pick_ignores_live_chat_as_a_caption():
    only_chat = {"subtitles": {"live_chat": _tracks("chat")}}
    assert ytdl._pick_caption_track(only_chat) is None
    chat_first = {"subtitles": {"live_chat": _tracks("chat"), "fr": _tracks("manual-fr")}}
    assert ytdl._pick_caption_track(chat_first) == "manual-fr"


def test_pick_skips_a_track_without_json3_and_moves_to_the_next_priority():
    info = {
        "language": "en",
        "subtitles": {"en": [{"ext": "vtt", "url": "manual-en.vtt"}]},
        "automatic_captions": {"en-orig": _tracks("asr-en-orig")},
    }
    assert ytdl._pick_caption_track(info) == "asr-en-orig"


def test_pick_skips_json3_entry_without_a_url():
    info = {
        "language": "en",
        "subtitles": {"en": [{"ext": "json3", "url": ""}, {"ext": "json3"}]},
        "automatic_captions": {"en": _tracks("asr-en")},
    }
    assert ytdl._pick_caption_track(info) == "asr-en"


def test_pick_never_falls_back_to_a_translated_asr_track():
    info = {
        "language": "en",
        "subtitles": {},
        "automatic_captions": {"fr": _tracks("t-fr"), "de": _tracks("t-de"), "ja": _tracks("t-ja")},
    }
    assert ytdl._pick_caption_track(info) is None


@pytest.mark.parametrize(
    "info",
    [
        {},
        {"language": "en"},
        {"subtitles": None, "automatic_captions": None},
        {"language": "en", "subtitles": {"en": None}, "automatic_captions": {"en-orig": "junk"}},
        {"language": "en", "subtitles": {"en": []}, "automatic_captions": {"en": [None, 3]}},
    ],
)
def test_pick_none_when_nothing_qualifies_or_input_is_malformed(info):
    assert ytdl._pick_caption_track(info) is None


# --------------------------------------------------------------------------- #
# video_transcript / _extract_with_captions (I/O boundary patched)
# --------------------------------------------------------------------------- #
PAYLOAD = {"events": [{"tStartMs": 1000, "segs": [{"utf8": "hello"}]}]}


def test_video_transcript_shape(monkeypatch):
    seen = []

    def fake(video_id):
        seen.append(video_id)
        return {"title": "A Title", "view_count": 1234567}, PAYLOAD

    monkeypatch.setattr(ytdl, "_extract_with_captions", fake)
    r = real_video_transcript(VID)
    assert seen == [VID]
    assert r == {
        "video_id": VID,
        "title": "A Title",
        "views_int": 1234567,
        "segments": [("0:01", "hello")],
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(1234567, 1234567), (12.0, 12), (1, 1), (0, None), (-5, None), (None, None), ("1.2M", None)],
)
def test_video_transcript_views_normalised_like_entry_to_item(monkeypatch, raw, expected):
    monkeypatch.setattr(
        ytdl, "_extract_with_captions", lambda vid: ({"title": "T", "view_count": raw}, None)
    )
    assert real_video_transcript(VID)["views_int"] == expected
    # Same normalisation as the channel listing (one rule across the module).
    assert ytdl._entry_to_item({"id": VID, "view_count": raw})["views_int"] == expected


def test_video_transcript_no_track_gives_empty_segments(monkeypatch):
    monkeypatch.setattr(ytdl, "_extract_with_captions", lambda vid: ({"title": "T"}, None))
    r = real_video_transcript(VID)
    assert r["segments"] == [] and r["title"] == "T" and r["views_int"] is None


def test_video_transcript_missing_title_is_empty_string(monkeypatch):
    monkeypatch.setattr(ytdl, "_extract_with_captions", lambda vid: ({"title": None}, PAYLOAD))
    assert real_video_transcript(VID)["title"] == ""


def test_video_transcript_garbage_payload_gives_empty_segments(monkeypatch):
    monkeypatch.setattr(ytdl, "_extract_with_captions", lambda vid: ({}, {"events": "nope"}))
    assert real_video_transcript(VID)["segments"] == []


def test_video_transcript_does_not_catch_exceptions(monkeypatch):
    def boom(video_id):
        raise RuntimeError("network down")

    monkeypatch.setattr(ytdl, "_extract_with_captions", boom)
    with pytest.raises(RuntimeError, match="network down"):
        real_video_transcript(VID)


class _FakeYDL:
    """Stand-in for ``yt_dlp.YoutubeDL``: records what the boundary does with ONE instance."""

    instances: list[_FakeYDL] = []
    info: dict = {}
    raises: Exception | None = None
    # A STRICT instance is one built WITHOUT ``ignore_no_formats_error``: that is where yt-dlp
    # raises "unavailable"/"members-only"/"upcoming"; the lenient one returns an empty info.
    raises_strict: Exception | None = None
    info_strict: dict | None = None

    def __init__(self, opts):
        self.opts = opts
        self.extracted: list[str] = []
        self.opened: list[str] = []
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=True):
        assert download is False
        self.extracted.append(url)
        if self.raises:
            raise self.raises
        if not self.opts.get("ignore_no_formats_error"):
            if self.raises_strict:
                raise self.raises_strict
            if self.info_strict is not None:
                return self.info_strict
        return self.info

    def urlopen(self, url):
        self.opened.append(url)
        return io.BytesIO(json.dumps(PAYLOAD).encode())


@pytest.fixture
def fake_yt_dlp(monkeypatch):
    _FakeYDL.instances = []
    _FakeYDL.info = {}
    _FakeYDL.raises = None
    _FakeYDL.raises_strict = None
    _FakeYDL.info_strict = None
    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=_FakeYDL))
    return _FakeYDL


def test_extract_with_captions_uses_one_session_for_info_and_the_caption_file(fake_yt_dlp):
    fake_yt_dlp.info = {
        "title": "T",
        "language": "en",
        "subtitles": {"en": _tracks("https://cap/en")},
    }
    info, payload = ytdl._extract_with_captions(VID)
    assert info is fake_yt_dlp.info and payload == PAYLOAD
    (ydl,) = fake_yt_dlp.instances  # a single YoutubeDL does both calls
    assert ydl.extracted == [f"https://www.youtube.com/watch?v={VID}"]
    assert ydl.opened == ["https://cap/en"]
    assert ydl.opts["quiet"] is True and ydl.opts["skip_download"] is True
    assert isinstance(ydl.opts["logger"], ytdl._NullLogger)


def test_extract_with_captions_without_a_track_never_downloads(fake_yt_dlp):
    fake_yt_dlp.info = {"title": "T", "language": "en", "automatic_captions": {"fr": _tracks("x")}}
    info, payload = ytdl._extract_with_captions(VID)
    assert payload is None and info["title"] == "T"
    assert fake_yt_dlp.instances[0].opened == []


def test_extract_with_captions_tolerates_empty_info(fake_yt_dlp):
    fake_yt_dlp.info = None  # yt-dlp may hand back None
    assert ytdl._extract_with_captions(VID) == ({}, None)


def test_extract_with_captions_propagates_extractor_errors(fake_yt_dlp):
    fake_yt_dlp.raises = RuntimeError("video unavailable")
    with pytest.raises(RuntimeError, match="video unavailable"):
        ytdl._extract_with_captions(VID)


def test_extract_with_captions_first_pass_ignores_no_formats_errors(fake_yt_dlp):
    # An age-gated video fails on its FORMATS yet still lists its caption tracks (live check),
    # where it used to raise. Captions need no formats, so ask for none.
    fake_yt_dlp.info = {
        "title": "T",
        "language": "en",
        "subtitles": {"en": _tracks("https://cap/en")},
    }
    info, payload = ytdl._extract_with_captions(VID)
    (ydl,) = fake_yt_dlp.instances  # captions found: still ONE session, no second ask
    assert ydl.opts["ignore_no_formats_error"] is True
    assert payload == PAYLOAD and info["title"] == "T"


def test_extract_with_captions_ordinary_video_without_captions_is_not_asked_twice(fake_yt_dlp):
    fake_yt_dlp.info = {"title": "T", "formats": [{"format_id": "18"}]}  # playable, no captions
    info, payload = ytdl._extract_with_captions(VID)
    assert payload is None and info["title"] == "T"
    assert len(fake_yt_dlp.instances) == 1


def test_extract_with_captions_no_captions_and_no_formats_asks_again_strictly(fake_yt_dlp):
    # The flag turns "unavailable"/"members-only"/"upcoming" into an empty info (title
    # "youtube video #id"). Returning that would make `--transcript-engine ytdlp` report a
    # fake title with no segments instead of failing the video, so the contract is kept:
    # whatever the strict ask raises is what the caller gets.
    fake_yt_dlp.info = {"title": f"youtube video #{VID}"}
    fake_yt_dlp.raises_strict = RuntimeError("This video is available to this channel's members")
    with pytest.raises(RuntimeError, match="members"):
        ytdl._extract_with_captions(VID)
    lenient, strict = fake_yt_dlp.instances
    assert lenient.opts["ignore_no_formats_error"] is True
    assert "ignore_no_formats_error" not in strict.opts
    assert strict.extracted == [f"https://www.youtube.com/watch?v={VID}"]


def test_extract_with_captions_strict_ask_that_succeeds_may_still_find_the_captions(fake_yt_dlp):
    fake_yt_dlp.info = {"title": "T"}  # lenient pass: nothing (a flaky first answer)
    fake_yt_dlp.info_strict = {"title": "T", "language": "en", "subtitles": {"en": _tracks("c")}}
    info, payload = ytdl._extract_with_captions(VID)
    assert payload == PAYLOAD and info is fake_yt_dlp.info_strict
    lenient, strict = fake_yt_dlp.instances
    assert lenient.opened == [] and strict.opened == ["c"]


# --------------------------------------------------------------------------- #
# lazy-import contract (subprocess: a clean interpreter)
# --------------------------------------------------------------------------- #
def _run_py(code: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": SRC}
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)


def test_importing_ytdl_and_running_its_pure_helpers_never_imports_yt_dlp():
    r = _run_py(
        "import sys\n"
        "import viewlyt.ytdl as y\n"
        "assert y._pick_caption_track({'language': 'en'}) is None\n"
        "assert y._json3_to_segments({'events': []}) == []\n"
        "assert 'yt_dlp' not in sys.modules, 'yt_dlp imported by viewlyt.ytdl'\n"
        "print('OK')\n"
    )
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_vl_help_never_imports_yt_dlp():
    r = _run_py(
        "import sys\n"
        "from viewlyt.cli import main\n"
        "try:\n"
        "    main(['--help'])\n"
        "except SystemExit:\n"
        "    pass\n"
        "assert 'yt_dlp' not in sys.modules, 'yt_dlp imported on vl --help'\n"
        "print('OK')\n"
    )
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_help_documents_the_transcript_engine_flag():
    r = subprocess.run(
        [sys.executable, "-c", "import sys\nfrom viewlyt.cli import main\nmain(['--help'])\n"],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": SRC, "COLUMNS": "200"},
    )
    assert r.returncode == 0, r.stderr
    assert "--transcript-engine {auto,ytdlp,selenium}" in r.stdout


# --------------------------------------------------------------------------- #
# Orchestration through cli.run_batch / cli.main
# --------------------------------------------------------------------------- #
SEGS = [("0:00", "alpha"), ("0:02", "beta"), ("0:04", "gamma")]


class Rig:
    """Replaces the three boundaries ``run_batch`` crosses and records every use.

    * ``ytdl.video_transcript`` -> ``yt_calls``; behaviour set by :meth:`yt` / :meth:`yt_raises`
      (default: raises, like the autouse stub);
    * ``cli.build_primed_driver`` -> ``drivers`` (the ``headless`` flag of each build);
    * ``cli.scrape_one`` -> ``scrapes`` (the kwargs of each call); results come from
      :meth:`selenium` (a list consumed in order, the last one repeating; an exception
      instance in it is raised instead of returned).
    """

    def __init__(self, monkeypatch):
        self.yt_calls: list[str] = []
        self.drivers: list[bool] = []
        self.scrapes: list[dict] = []
        self._yt: dict | Exception = RuntimeError("yt-dlp disabled in tests")
        self._selenium: list = []
        monkeypatch.setattr(ytdl, "video_transcript", self._video_transcript)
        monkeypatch.setattr(cli, "build_primed_driver", self._build)
        monkeypatch.setattr(cli, "scrape_one", self._scrape)

    # -- configuration
    def yt(self, segments=SEGS, title="YT Title", views=1_234_567) -> Rig:
        self._yt = {"title": title, "views_int": views, "segments": list(segments)}
        return self

    def yt_raises(self, exc: Exception | None = None) -> Rig:
        self._yt = exc or RuntimeError("yt-dlp exploded")
        return self

    def selenium(self, *results) -> Rig:
        self._selenium = list(results)
        return self

    # -- fakes
    def _video_transcript(self, video_id):
        self.yt_calls.append(video_id)
        if isinstance(self._yt, Exception):
            raise self._yt
        return {"video_id": video_id, **self._yt}

    def _build(self, headless, user_data_dir):
        self.drivers.append(headless)
        return FakeDriver()

    def _scrape(self, driver, url, **kw):
        self.scrapes.append(kw)
        if not self._selenium:
            raise AssertionError("scrape_one must not run in this test")
        item = self._selenium.pop(0) if len(self._selenium) > 1 else self._selenium[0]
        if isinstance(item, Exception):
            raise item
        return item


def sel(
    title="Selenium Title", records=(), transcript=(), related=(), views=None, vid=VID
) -> tuple:
    """A ``scrape_one`` 6-tuple."""
    return (vid, title, list(records), list(transcript), list(related), views)


@pytest.fixture
def rig(monkeypatch):
    return Rig(monkeypatch)


def _run_batch(tmp_path, targets=((VID, VID),), **overrides):
    kw = dict(
        jobs=1,
        headless=True,
        fallback=True,
        user_data_dir=None,
        out_dir=str(tmp_path),
        limit=150,
        max_viewports=25,
        expand_replies=True,
        max_replies=5,
        with_comments=False,
        with_transcript=True,
        with_related=False,
        related_limit=0,
        merge_comments=True,
        unify=False,
        unify_all=False,
        inner_progress=False,
        quiet=True,
    )
    kw.update(overrides)
    return cli.run_batch(list(targets), **kw)


def _main(tmp_path, *argv: str) -> int:
    return cli.main(["-o", str(tmp_path), "--no-tokens", *argv])


def _tfile(tmp_path, title="YT Title", vid=VID):
    return tmp_path / f"{slugify(title)}-{vid}.transcript.md"


# -- transcript-only: no browser at all --------------------------------------
def test_transcript_only_from_ytdlp_never_builds_a_driver(rig, tmp_path, capsys):
    rig.yt()
    rc = _main(tmp_path, VID)
    assert rc == 0
    assert rig.drivers == [] and rig.scrapes == []
    assert rig.yt_calls == [VID]
    assert _tfile(tmp_path).read_text(encoding="utf-8") == "alpha beta\ngamma\n"  # lean default
    out = capsys.readouterr().out
    assert "Done: 1/1" in out
    assert f"transcript: 3 segments -> {_tfile(tmp_path)}" in out
    assert "1,234,567 views" in out  # exact views came from yt-dlp too


def test_ts_flag_keeps_the_stamps_from_ytdlp_segments(rig, tmp_path):
    rig.yt()
    assert _main(tmp_path, "--ts", VID) == 0
    assert (
        _tfile(tmp_path).read_text(encoding="utf-8") == "[0:00] alpha [0:02] beta\n[0:04] gamma\n"
    )


def test_default_strips_stamps_but_keeps_hour_stamps(rig, tmp_path):
    # strip_timestamps only removes [m:ss]/[mm:ss]; h:mm:ss survives by design.
    rig.yt(segments=[("0:01", "a"), ("1:02:03", "b")])
    assert _main(tmp_path, VID) == 0
    assert _tfile(tmp_path).read_text(encoding="utf-8") == "a [1:02:03] b\n"


def test_summary_records_the_engine_and_views_from_ytdlp(rig, tmp_path):
    rig.yt()
    (s,) = _run_batch(tmp_path)
    assert s["error"] is None
    assert s["transcript_engine"] == "yt-dlp"
    assert s["video_id"] == VID and s["title"] == "YT Title" and s["views_int"] == 1_234_567
    assert s["segments"] == 3 and s["transcript_file"] == str(_tfile(tmp_path))


def test_empty_ytdlp_title_keeps_the_existing_video_slug(rig, tmp_path):
    rig.yt(title="")
    assert _main(tmp_path, VID) == 0
    assert (tmp_path / f"video-{VID}.transcript.md").exists()  # slugify("") == "video"


def test_several_videos_mixed_engines_in_one_batch(rig, monkeypatch, tmp_path):
    other = "abcdefghij_"
    # First video served by yt-dlp; the second one's yt-dlp call fails -> Selenium.
    answers = {VID: None, other: RuntimeError("blocked")}

    def vt(video_id):
        rig.yt_calls.append(video_id)
        if answers[video_id]:
            raise answers[video_id]
        return {"video_id": video_id, "title": "Via YT", "views_int": 5, "segments": SEGS}

    monkeypatch.setattr(ytdl, "video_transcript", vt)
    rig.selenium(sel(title="Via Sel", transcript=[("0:00", "sel")], vid=other))
    sums = _run_batch(tmp_path, targets=[(VID, VID), (other, other)])
    assert [s["video_id"] for s in sums] == [VID, other]
    assert [s["transcript_engine"] for s in sums] == ["yt-dlp", "selenium"]
    assert (
        len(rig.drivers) == 1 and len(rig.scrapes) == 1
    )  # only the fallback video paid for Chrome


# -- fallbacks (auto) ---------------------------------------------------------
def test_auto_falls_back_to_selenium_when_ytdlp_raises(rig, tmp_path):
    rig.yt_raises().selenium(sel(transcript=[("0:00", "from panel"), ("0:02", "text")]))
    assert _main(tmp_path, VID) == 0
    assert rig.yt_calls == [VID]  # tried once, no retry of the video: the fallback succeeded
    assert rig.drivers == [True]
    assert rig.scrapes[0]["with_transcript"] is True  # the Selenium path is unchanged
    assert _tfile(tmp_path, "Selenium Title").read_text(encoding="utf-8") == "from panel text\n"


def test_auto_falls_back_to_selenium_when_ytdlp_has_no_segments(rig, tmp_path):
    rig.yt(segments=[]).selenium(sel(transcript=[("0:00", "panel")]))
    assert _main(tmp_path, VID) == 0
    assert rig.drivers == [True] and rig.scrapes[0]["with_transcript"] is True
    assert _tfile(tmp_path, "Selenium Title").read_text(encoding="utf-8") == "panel\n"


def test_summary_engine_is_selenium_after_a_fallback(rig, tmp_path):
    rig.yt_raises().selenium(sel(transcript=[("0:00", "panel")]))
    (s,) = _run_batch(tmp_path)
    assert s["transcript_engine"] == "selenium" and s["segments"] == 1


def test_selenium_fallback_with_no_transcript_either_is_unavailable_not_a_failure(rig, tmp_path):
    rig.yt(segments=[]).selenium(sel(transcript=[]))
    (s,) = _run_batch(tmp_path)
    assert s["error"] is None and s["segments"] == 0 and s["transcript_file"] is None
    assert s["transcript_engine"] == "selenium"


# -- --transcript-engine selenium (the old behaviour) -------------------------
def test_engine_selenium_never_asks_ytdlp(rig, tmp_path):
    rig.yt().selenium(sel(transcript=[("0:00", "panel")]))
    assert _main(tmp_path, "--transcript-engine", "selenium", VID) == 0
    assert rig.yt_calls == []
    assert rig.drivers == [True] and rig.scrapes[0]["with_transcript"] is True
    assert _tfile(tmp_path, "Selenium Title").read_text(encoding="utf-8") == "panel\n"


def test_engine_selenium_summary(rig, tmp_path):
    rig.selenium(sel(transcript=[("0:00", "panel")]))
    (s,) = _run_batch(tmp_path, transcript_engine="selenium")
    assert s["transcript_engine"] == "selenium"


# -- --transcript-engine ytdlp -------------------------------------------------
def test_engine_ytdlp_with_zero_segments_is_unavailable_and_opens_no_browser(rig, tmp_path, capsys):
    rig.yt(segments=[], title="No Captions", views=99)
    rc = _main(tmp_path, "--transcript-engine", "ytdlp", VID)
    assert rc == 0
    assert rig.drivers == [] and rig.scrapes == []
    assert list(tmp_path.glob("*.md")) == []  # no 0-byte file
    out = capsys.readouterr().out
    assert "Done: 1/1" in out and "transcript: unavailable" in out
    assert "99 views" in out


def test_engine_ytdlp_zero_segments_summary(rig, tmp_path):
    rig.yt(segments=[])
    (s,) = _run_batch(tmp_path, transcript_engine="ytdlp")
    assert s["error"] is None and s["transcript_engine"] == "yt-dlp" and s["segments"] == 0


def test_engine_ytdlp_with_an_exception_fails_the_video_after_its_retry(rig, tmp_path, capsys):
    rig.yt_raises(RuntimeError("HTTP 429")).selenium(sel(transcript=[("0:00", "must not be used")]))
    rc = _main(tmp_path, "--transcript-engine", "ytdlp", VID)
    assert rc == 1
    assert rig.yt_calls == [VID, VID]  # first attempt + the one automatic retry
    assert rig.drivers == [] and rig.scrapes == []  # Selenium is never the transcript's plan B
    cap = capsys.readouterr()
    assert "Done: 0/1" in cap.out and "HTTP 429" in cap.err
    assert list(tmp_path.glob("*.md")) == []


def test_engine_ytdlp_exception_with_comments_also_fails_the_video(rig, tmp_path):
    rig.yt_raises(RuntimeError("HTTP 429")).selenium(sel(records=[_c("@a", "hi")]))
    (s,) = _run_batch(tmp_path, transcript_engine="ytdlp", with_comments=True)
    assert s["error"] == "HTTP 429" and rig.scrapes == []


def test_engine_ytdlp_with_comments_and_zero_segments_still_scrapes_comments(rig, tmp_path):
    rig.yt(segments=[]).selenium(sel(records=[_c("@a", "hi")]))
    (s,) = _run_batch(tmp_path, transcript_engine="ytdlp", with_comments=True)
    assert s["error"] is None and s["comments"] == 1 and s["transcript_engine"] == "yt-dlp"
    assert rig.scrapes[0]["with_transcript"] is False  # never the panel under --engine ytdlp
    assert s["transcript_file"] is None


# -- comments / related selected: the browser is needed anyway ---------------
def test_comments_plus_transcript_scrapes_without_the_panel_and_uses_ytdlp_text(
    rig, tmp_path, capsys
):
    recs = [_c("@joao", "ola", likes="5", date_raw="2 days ago")]
    rig.yt().selenium(sel(title="Selenium Title", records=recs, views=777))
    rc = _main(tmp_path, "-c", "-t", VID)
    assert rc == 0
    (kw,) = rig.scrapes
    assert kw["with_transcript"] is False and kw["with_comments"] is True
    assert rig.drivers == [True]
    # Title/views/slug come from the page scrape; the transcript text from yt-dlp.
    cfile = tmp_path / f"{slugify('Selenium Title')}-{VID}.md"
    tfile = _tfile(tmp_path, "Selenium Title")
    assert cfile.read_text(encoding="utf-8") == (
        "\n".join(cli.format_comment_lines(recs, progress=False, merge_comments=True)) + "\n"
    )
    assert tfile.read_text(encoding="utf-8") == "alpha beta\ngamma\n"
    out = capsys.readouterr().out
    assert "777 views" in out and "1,234,567" not in out


def test_comments_plus_transcript_summary_engine_is_ytdlp(rig, tmp_path):
    rig.yt().selenium(sel(records=[_c("@a", "x")]))
    (s,) = _run_batch(tmp_path, with_comments=True)
    assert s["transcript_engine"] == "yt-dlp" and s["segments"] == 3 and s["comments"] == 1


def test_comments_plus_transcript_falls_back_to_the_panel_when_ytdlp_fails(rig, tmp_path):
    rig.yt_raises().selenium(sel(records=[_c("@a", "x")], transcript=[("0:00", "panel")]))
    (s,) = _run_batch(tmp_path, with_comments=True)
    assert rig.scrapes[0]["with_transcript"] is True
    assert s["transcript_engine"] == "selenium" and s["segments"] == 1


def test_related_selected_also_needs_the_browser(rig, tmp_path):
    rel = [
        {"title": "R", "views": "1M views", "url": "https://www.youtube.com/watch?v=zzzzzzzzzzz"}
    ]
    rig.yt().selenium(sel(related=rel))
    (s,) = _run_batch(tmp_path, with_related=True, related_limit=3)
    (kw,) = rig.scrapes
    assert kw["with_transcript"] is False and kw["with_related"] is True
    assert s["related"] == 1 and s["transcript_engine"] == "yt-dlp" and s["segments"] == 3


def test_unify_carries_the_ytdlp_transcript(rig, tmp_path):
    recs = [_c("@a", "hi", likes="5")]
    rig.yt().selenium(sel(title="Page Title", records=recs, views=42))
    rc = _main(tmp_path, "-u", VID)
    assert rc == 0
    assert rig.scrapes[0]["with_transcript"] is False  # unify alone = everything, via the page
    body = (tmp_path / f"{slugify('Page Title')}-{VID}.unified.md").read_text(encoding="utf-8")
    assert body.startswith("# Page Title\n")
    assert "42 views" in body
    assert "## Transcript\n\nalpha beta\ngamma\n" in body
    assert "## Comments" in body


def test_unify_transcript_only_has_no_browser_and_a_ytdlp_subtitle(rig, tmp_path):
    rig.yt(title="YT Title", views=1_000)
    rc = _main(tmp_path, "-t", "-u", VID)
    assert rc == 0
    assert rig.drivers == [] and rig.scrapes == []
    body = (tmp_path / f"{slugify('YT Title')}-{VID}.unified.md").read_text(encoding="utf-8")
    assert body.splitlines()[:3] == ["# YT Title", "", "1,000 views"]
    assert "## Transcript\n\nalpha beta\ngamma\n" in body


def test_unify_all_with_ytdlp_transcripts_in_input_order(rig, tmp_path):
    rig.yt()
    other = "abcdefghij_"
    rc = _main(tmp_path, "-t", "--unify-all", "-j", "1", VID, other)
    assert rc == 0
    assert rig.drivers == []
    doc = (tmp_path / cli.UNIFIED_ALL_FILENAME).read_text(encoding="utf-8")
    assert doc.count("## Transcript") == 2


# -- regression guards for the refactored browse() helper --------------------
def test_block_fallback_to_headed_still_wraps_the_page_scrape(rig, tmp_path):
    rig.yt().selenium(BlockedError("consent"), sel(records=[_c("@a", "hi")]))
    (s,) = _run_batch(tmp_path, with_comments=True)
    assert s["error"] is None and s["transcript_engine"] == "yt-dlp"
    assert rig.drivers == [True, False]  # rebuilt headed after the block
    assert [kw["with_transcript"] for kw in rig.scrapes] == [False, False]


def test_block_with_fallback_disabled_fails_the_video(rig, tmp_path):
    rig.yt().selenium(BlockedError("consent"))
    (s,) = _run_batch(tmp_path, with_comments=True, fallback=False)
    assert s["error"] and rig.drivers == [
        True,
        True,
    ]  # first try + the per-video retry, both headless


def test_no_transcript_requested_never_asks_ytdlp_and_reports_no_engine(rig, tmp_path):
    rig.yt().selenium(sel(records=[_c("@a", "hi")]))
    (s,) = _run_batch(tmp_path, with_comments=True, with_transcript=False)
    assert rig.yt_calls == []
    assert s["transcript_engine"] is None and s["error"] is None
    assert rig.scrapes[0]["with_transcript"] is False


def test_ytdlp_runs_once_per_attempt_and_a_failed_page_scrape_is_retried(rig, tmp_path):
    rig.yt().selenium(RuntimeError("stale session"), sel(records=[_c("@a", "hi")]))
    (s,) = _run_batch(tmp_path, with_comments=True)
    assert s["error"] is None and s["transcript_engine"] == "yt-dlp"
    assert rig.yt_calls == [VID, VID]  # once per attempt; the retry is a whole new attempt


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #
def test_transcript_engine_defaults_to_auto_and_accepts_the_three_choices():
    p = cli.build_parser()
    assert p.parse_args([VID]).transcript_engine == "auto"
    for choice in ("auto", "ytdlp", "selenium"):
        assert p.parse_args(["--transcript-engine", choice, VID]).transcript_engine == choice


def test_transcript_engine_rejects_unknown_values(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["--transcript-engine", "magic", VID])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_main_passes_the_engine_to_run_batch(monkeypatch):
    cap = {}

    def fake_run_batch(targets, **kw):
        cap.update(kw)
        return [{"video_id": t[0], "error": None} for t in targets]

    monkeypatch.setattr(cli, "run_batch", fake_run_batch)
    assert cli.main(["--no-tokens", "--transcript-engine", "ytdlp", VID]) == 0
    assert cap["transcript_engine"] == "ytdlp"
    cap.clear()
    assert cli.main(["--no-tokens", VID]) == 0
    assert cap["transcript_engine"] == "auto"
