"""Browser-free tests for the pure RAG-prep helpers. Run: `uv run python tests/test_rag.py`.

Covers only the dependency-free document-shaping layer of :mod:`viewlyt.rag`
(filename parsing, count parsing, engagement metrics, document assembly). The
LightRAG ingest/query layer is I/O + third-party and is not exercised here.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from datetime import UTC

from viewlyt.rag import (  # noqa: E402
    RagConfig,
    RagDocument,
    _chat_messages,
    _split_inputs,
    build_ask_parser,
    build_chat_context,
    build_document,
    comment_metrics,
    parse_count,
    parse_out_filename,
)

ID = "dQw4w9WgXcQ"

# A small but realistic comments-file body (the exact shape format_comment_lines emits).
COMMENTS_MD = "\n".join(
    [
        "@joao [842 likes, 2026-06-04]: Melhor vídeo do canal",
        "    ↳ (in reply to @joao) @maria [12 likes, 2026-06-03]: concordo",
        "    ↳ (in reply to @joao) @ana [0 likes, 2026-06-03]: idem",
        "",
        "@pedro [1.2K likes, 2026-06-01]: explicação top",
        "",
        "unknown [3 likes, unknown]: sem autor",
    ]
)


def test_parse_out_filename() -> None:
    assert parse_out_filename(f"rick-astley-{ID}.md") == ("rick-astley", ID, "comments")
    assert parse_out_filename(f"rick-astley-{ID}.transcript.md") == (
        "rick-astley",
        ID,
        "transcript",
    )
    assert parse_out_filename(f"rick-astley-{ID}.related.md") == ("rick-astley", ID, "related")
    assert parse_out_filename(f"rick-astley-{ID}.unified.md") == ("rick-astley", ID, "unified")
    # a directory prefix is stripped; the slug can itself contain hyphens
    assert parse_out_filename(f"out/meu-video-legal-{ID}.md") == ("meu-video-legal", ID, "comments")
    # no recognizable 11-char id (the --unify-all global file) -> empty id, whole base is slug
    assert parse_out_filename("unified-all.md") == ("unified-all", "", "unified")
    # ids may contain '-'/'_' (still 11 chars)
    assert parse_out_filename("clip-ab_cd-ef_gh.md") == ("clip", "ab_cd-ef_gh", "comments")
    print("ok: parse_out_filename")


def test_parse_count() -> None:
    assert parse_count("842") == 842
    assert parse_count("0") == 0
    assert parse_count("") == 0
    assert parse_count("1.2K") == 1200
    assert parse_count("12K") == 12000
    assert parse_count("3.4M") == 3_400_000
    assert parse_count("1.2B") == 1_200_000_000
    # pt-BR magnitude words and decimal comma
    assert parse_count("1,2 mil") == 1200
    assert parse_count("2 mi") == 2_000_000
    # bare number with a thousands grouping (no suffix) -> separators stripped
    assert parse_count("1,234") == 1234
    assert parse_count("1.234") == 1234
    # junk -> 0, never raises
    assert parse_count("abc") == 0
    assert parse_count("K") == 0
    print("ok: parse_count")


def test_comment_metrics() -> None:
    m = comment_metrics(COMMENTS_MD)
    assert m["comments"] == 3, m  # @joao, @pedro, unknown (top-level)
    assert m["replies"] == 2, m  # @maria, @ana
    # 842 + 12 + 0 + 1200 + 3
    assert m["total_likes"] == 842 + 12 + 0 + 1200 + 3, m
    assert m["top_likes"] == 1200, m  # the 1.2K comment
    # transcript/related/blank text has no like stamps -> all zeros
    empty = comment_metrics("[0:00] hello\n[0:02] world\n\n1. [5 views. T](u)")
    assert empty == {"comments": 0, "replies": 0, "total_likes": 0, "top_likes": 0}, empty
    print("ok: comment_metrics")


def test_build_document_comments() -> None:
    doc = build_document(f"out/rick-astley-{ID}.md", COMMENTS_MD)
    assert isinstance(doc, RagDocument)
    assert doc.doc_id == f"rick-astley-{ID}.md"  # basename, not the full path
    assert doc.file_path == f"out/rick-astley-{ID}.md"
    assert doc.video_id == ID and doc.kind == "comments"
    assert doc.title == "rick astley"  # de-slugified (no # heading in a comments file)
    # header identifies the video and carries the engagement tally...
    assert doc.content.startswith("# rick astley\n")
    assert f"- url: https://www.youtube.com/watch?v={ID}" in doc.content
    assert "- video_id: " + ID in doc.content
    assert "- comments collected: 3" in doc.content
    assert "- total likes (approx): 2057" in doc.content
    # ...and the original body is preserved after the header
    assert "Melhor vídeo do canal" in doc.content
    assert "(in reply to @joao) @maria" in doc.content
    print("ok: build_document_comments")


def test_build_document_unified_dedups_title() -> None:
    # A unified body already opens with a real "# Título" heading; build_document must
    # use it as the title and NOT leave a duplicate heading in the body.
    unified = "\n".join(
        [
            "# Atenção: OVNIs à Noite",
            "",
            "## Comments",
            "",
            "@x [10 likes, 2026-06-01]: incrível",
            "",
            "## Transcript",
            "",
            "[0:00] olá",
        ]
    )
    doc = build_document(f"out/atencao-ovnis-{ID}.unified.md", unified)
    assert doc.kind == "unified"
    assert doc.title == "Atenção: OVNIs à Noite"  # the accented heading wins over the slug
    # exactly one top-level "# " heading (the one we added), the body's was dropped
    assert doc.content.count("\n# ") == 0 and doc.content.startswith("# Atenção: OVNIs à Noite")
    assert doc.content.count("# Atenção: OVNIs à Noite") == 1
    # metrics are computed for unified too
    assert "- comments collected: 1" in doc.content
    # the body sections survive
    assert "## Comments" in doc.content and "[0:00] olá" in doc.content
    print("ok: build_document_unified_dedups_title")


def test_build_document_transcript_has_no_metrics() -> None:
    # Transcript/related products carry no like stamps -> no engagement bullets.
    doc = build_document(f"out/clip-{ID}.transcript.md", "[0:00] hi\n[0:02] there")
    assert doc.kind == "transcript"
    assert "comments collected" not in doc.content
    assert doc.content.startswith("# clip\n")
    assert "[0:00] hi" in doc.content
    print("ok: build_document_transcript_has_no_metrics")


def test_build_document_no_video_id() -> None:
    # The --unify-all global file has no per-video id -> no video_id/url metadata lines.
    doc = build_document("out/unified-all.md", "# V1\n\n## Comments\n\n@a [1 likes, x]: oi")
    assert doc.video_id == "" and doc.kind == "unified"
    assert "- video_id:" not in doc.content and "- url:" not in doc.content
    assert doc.title == "V1"  # first heading
    print("ok: build_document_no_video_id")


def test_ragconfig_from_env_defaults() -> None:
    cfg = RagConfig.from_env({})  # nothing set
    assert cfg.llm_base_url == "https://openrouter.ai/api/v1"
    assert cfg.llm_api_key == "" and cfg.llm_model  # key empty, model has a default
    # embeddings are local by default -> no key, multilingual model, dim 1024
    assert cfg.embed_provider == "fastembed" and cfg.embed_api_key == ""
    assert cfg.embed_dim == 1024 and "e5" in cfg.embed_model
    # cost knobs: cheap-extract off (reuse llm_model), gleaning off, default chunk size
    assert cfg.extract_model == "" and cfg.max_gleaning == 0 and cfg.chunk_tokens is None
    print("ok: ragconfig_from_env_defaults")


def test_ragconfig_from_env_openrouter_llm() -> None:
    cfg = RagConfig.from_env({"OPENROUTER_API_KEY": "sk-or-test", "LLM_NAME": "openai/gpt-4o-mini"})
    assert cfg.llm_api_key == "sk-or-test" and cfg.llm_model == "openai/gpt-4o-mini"
    assert cfg.embed_provider == "fastembed" and cfg.embed_api_key == ""  # embeddings stay local
    print("ok: ragconfig_from_env_openrouter_llm")


def test_ragconfig_from_env_embedding_providers() -> None:
    # openai embeddings: defaults + key falls back to OPENROUTER_API_KEY
    oa = RagConfig.from_env({"EMBEDDING_PROVIDER": "openai", "OPENROUTER_API_KEY": "k"})
    assert oa.embed_model == "text-embedding-3-small" and oa.embed_dim == 1536
    assert oa.embed_base_url == "https://api.openai.com/v1" and oa.embed_api_key == "k"
    # ollama: local base, no key
    ol = RagConfig.from_env({"EMBEDDING_PROVIDER": "ollama"})
    assert ol.embed_base_url == "http://localhost:11434/v1" and ol.embed_dim == 768
    # explicit overrides win over the per-provider defaults
    ov = RagConfig.from_env(
        {"EMBEDDING_PROVIDER": "openai", "EMBEDDING_NAME": "m", "EMBEDDING_DIM": "256"}
    )
    assert ov.embed_model == "m" and ov.embed_dim == 256
    print("ok: ragconfig_from_env_embedding_providers")


def test_ragconfig_cost_knobs() -> None:
    # the ingestion cost levers come from env and override the cheap defaults
    cfg = RagConfig.from_env(
        {
            "LLM_EXTRACT_NAME": "openai/gpt-4o-mini",
            "RAG_MAX_GLEANING": "1",
            "RAG_CHUNK_TOKENS": "2400",
        }
    )
    assert cfg.extract_model == "openai/gpt-4o-mini"
    assert cfg.max_gleaning == 1 and cfg.chunk_tokens == 2400
    print("ok: ragconfig_cost_knobs")


def test_build_chat_context() -> None:
    docs = [
        RagDocument("a.md", "out/a.md", "A", "id1", "comments", "# A\n\ncorpo do A"),
        RagDocument("b.md", "out/b.md", "B", "id2", "transcript", "# B\n\ncorpo do B"),
    ]
    ctx = build_chat_context(docs)
    assert "corpo do A" in ctx and "corpo do B" in ctx and "# A" in ctx and "# B" in ctx
    assert build_chat_context([]) == ""
    print("ok: build_chat_context")


def test_chat_messages() -> None:
    cfg = RagConfig.from_env({"VIEWLYT_RAG_LANG": "English"})
    msgs = _chat_messages(cfg, "DATA-BLOCK-XYZ", [{"role": "user", "content": "hi"}])
    assert msgs[0]["role"] == "system"
    system = msgs[0]["content"]
    # prompt hardening: the collected text is untrusted DATA, not instructions
    assert "UNTRUSTED DATA" in system and "NEVER follow" in system
    assert "English" in system  # answer language injected
    # secperf S12: the untrusted context lives in a tagged USER turn, NOT the system role
    assert "DATA-BLOCK-XYZ" not in system
    data_turn = msgs[1]
    assert data_turn["role"] == "user"
    assert "<collected_data>" in data_turn["content"]
    assert "DATA-BLOCK-XYZ" in data_turn["content"]
    assert msgs[2] == {"role": "user", "content": "hi"}  # history follows the data turn
    print("ok: chat_messages")


def test_rag_llm_func_appends_injection_guard(monkeypatch) -> None:
    # secperf S8: EVERY LightRAG LLM call (entity extraction on ingest + query on ask)
    # must carry the untrusted-data guard, appended to any base system prompt.
    import asyncio
    import sys
    import types

    from viewlyt.rag import _RAG_GUARD, RagConfig, _make_llm_func

    captured: dict = {}

    async def fake_complete(model, prompt, *, system_prompt=None, history_messages=None, **kw):
        captured["system_prompt"] = system_prompt
        return "ok"

    mod = types.ModuleType("lightrag.llm.openai")
    mod.openai_complete_if_cache = fake_complete  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "lightrag", types.ModuleType("lightrag"))
    monkeypatch.setitem(sys.modules, "lightrag.llm", types.ModuleType("lightrag.llm"))
    monkeypatch.setitem(sys.modules, "lightrag.llm.openai", mod)

    func = _make_llm_func(RagConfig.from_env({}))
    asyncio.run(func("prompt", system_prompt="EXTRACT ENTITIES"))
    assert "UNTRUSTED DATA" in captured["system_prompt"]
    assert "EXTRACT ENTITIES" in captured["system_prompt"]  # original prompt preserved
    asyncio.run(func("prompt", system_prompt=None))
    assert _RAG_GUARD in captured["system_prompt"]  # guard applied even with no base prompt


def test_expired_doc_ids() -> None:
    from datetime import datetime

    from viewlyt.rag import _expired_doc_ids

    now = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)
    created = {
        "old.md": "2026-06-01T00:00:00+00:00",  # 17d before now
        "fresh.md": "2026-06-10T00:00:00",  # 8d before, naive -> treated as UTC
        "edge.md": "2026-06-03T12:00:00+00:00",  # exactly 15d -> NOT strictly older
        "junk.md": "not-a-date",  # unparseable -> never purged (kept on error)
    }
    assert _expired_doc_ids(created, now, 15) == ["old.md"]
    assert _expired_doc_ids(created, now, 0) == []  # ttl 0 disables expiry
    assert set(_expired_doc_ids(created, now, 5)) == {"old.md", "fresh.md", "edge.md"}
    print("ok: expired_doc_ids")


def test_split_inputs() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        f1, f2 = Path(d) / "a.md", Path(d) / "b.md"
        f1.write_text("x", encoding="utf-8")
        f2.write_text("y", encoding="utf-8")
        # existing files -> paths; everything else joins into the question
        paths, q = _split_inputs([str(f1), str(f2), "qual", "teve", "mais", "aceitação?"])
        assert paths == [str(f1), str(f2)] and q == "qual teve mais aceitação?"
        # only a question (no files), and only files (no question)
        assert _split_inputs(["como", "se", "relacionam?"]) == ([], "como se relacionam?")
        assert _split_inputs([str(f1)]) == ([str(f1)], "")
    print("ok: split_inputs")


def test_build_ask_parser() -> None:
    p = build_ask_parser()
    d = p.parse_args([])
    assert d.mode == "mix" and d.store == "out/.rag"
    assert d.model is None and d.lang is None and d.quiet is False
    assert d.extract_model is None
    assert d.persist is False  # ephemeral chat is the default; LightRAG is opt-in
    a = p.parse_args(
        [
            "out/x.md",
            "pergunta",
            "--mode",
            "hybrid",
            "--model",
            "openai/gpt-4o",
            "--extract-model",
            "google/gemini-2.5-flash-lite",
            "--lang",
            "English",
        ]
    )
    assert a.mode == "hybrid" and a.model == "openai/gpt-4o" and a.lang == "English"
    assert a.extract_model == "google/gemini-2.5-flash-lite"
    assert a.inputs == ["out/x.md", "pergunta"]
    try:  # an unknown --mode is rejected by argparse (choices)
        p.parse_args(["--mode", "bogus"])
    except SystemExit:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected --mode to reject an unknown value")
    assert p.parse_args(["--persist", "out/x.md"]).persist is True
    assert isinstance(d.ttl_days, int)  # default from $RAG_TTL_DAYS or 15
    assert p.parse_args(["--ttl-days", "7"]).ttl_days == 7
    assert p.parse_args(["--ttl-days", "0"]).ttl_days == 0  # 0 = keep all
    print("ok: build_ask_parser")


if __name__ == "__main__":
    test_parse_out_filename()
    test_parse_count()
    test_comment_metrics()
    test_build_document_comments()
    test_build_document_unified_dedups_title()
    test_build_document_transcript_has_no_metrics()
    test_build_document_no_video_id()
    test_ragconfig_from_env_defaults()
    test_ragconfig_from_env_openrouter_llm()
    test_ragconfig_from_env_embedding_providers()
    test_ragconfig_cost_knobs()
    test_build_chat_context()
    test_chat_messages()
    test_expired_doc_ids()
    test_split_inputs()
    test_build_ask_parser()
    print("ALL TESTS PASSED")
