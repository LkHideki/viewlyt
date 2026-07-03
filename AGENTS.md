# viewlyt

CLI que coleta os comentários de um vídeo do YouTube (com likes, datas e respostas)
em `out/<title-slug>-<video_id>.md`, usando Selenium + Google Chrome headless,
gerenciado com `uv`. Veja @README.md para o uso completo.

## Comandos

```bash
uv sync                                             # cria o ambiente e instala as deps (Python >= 3.11; dev em 3.14)
uv run viewlyt '<url-do-youtube>'                # DEFAULT = só a transcrição -> *.transcript.md
uv run viewlyt '<url1>' '<url2>'                 # vários vídeos (pool de instâncias reutilizadas)
uv run viewlyt --from-file urls.txt -j 4         # de um .txt/.csv, 4 navegadores em paralelo
uv run viewlyt -c '<url>'                         # só comentários -> out/<slug>-<id>.md
uv run viewlyt -c -t '<url>'                     # comentários + transcrição
uv run viewlyt -c --limit-comments 150 --limit-replies 5 '<url>'  # (--limit/--max-replies são aliases)
uv run viewlyt -t '<url>'                        # só a transcrição (== default == --transcript-only)
uv run viewlyt -t --no-ts '<url>'                # transcrição sem os timestamps [m:ss] (h:mm:ss fica)
uv run viewlyt -r 17 '<url>'                     # 17 vídeos relacionados -> *.related.md (views, não likes)
uv run viewlyt -u '<url>'                         # todos os produtos -> *.unified.md (1 arquivo; alias de --unify)
uv run viewlyt -u --copy '<url>'                 # unifica e copia o conteúdo para a área de transferência
uv run viewlyt --unify-all '<url1>' '<url2>'     # todos os vídeos -> out/unified-all.md (1 arquivo só)
uv run viewlyt --no-merge-comments -c '<url>'    # não funde comentários consecutivos do mesmo autor
uv run viewlyt --headed '<url>'                  # navegador visível (melhor contra o bot wall)

uv run viewlyt-ask out/*.md 'qual vídeo teve mais aceitação?'  # chat efêmero sobre o já coletado (opt-in: uv sync --extra ask)
uv run viewlyt-ask --persist out/*.md '<pergunta>'  # base LightRAG persistente c/ janela de 15 dias (uv sync --extra rag)

uv run pytest                                       # toda a suíte (sem navegador; e2e pulado)
VIEWLYT_E2E=1 uv run pytest -m e2e                  # e2e real (Chrome + rede), opt-in
uv run ruff check --fix                             # lint
uv run ruff format                                  # formatação
uv run pre-commit install                           # roda ruff + pytest a cada commit
```

`uv sync` instala também o dependency-group `dev` (ruff, pytest, pre-commit).
As funções puras também rodam sem pytest via `uv run python tests/test_units.py`.

**Modo live (opt-in, `uv sync --extra live`):** `uv run viewlyt-live '<url-da-live>'`
sobe um servidor FastAPI + dashboard que analisa o chat ao vivo com LLM. O código vive
em `src/viewlyt/live/` (puro: `messages`/`window`/`probes`; I/O: `llm`/`server`/
`persistence`; dashboard Vite+TS em `dashboard/` → `static/`). Veja @how-to.md.

**Modo análise (opt-in):** `uv run viewlyt-ask out/*.md '<pergunta>'` dialoga com os `.md`
**já coletados** (sem re-coletar). **Padrão = chat efêmero** (`uv sync --extra ask`, só `openai`):
carrega os documentos no contexto e responde — **nada persiste**; com pergunta = one-shot, sem
pergunta = REPL. **`--persist`** (`uv sync --extra rag`) usa um índice **LightRAG** em `out/.rag/`
com **janela deslizante**: ao abrir, expurga documentos com mais de `--ttl-days` (default 15;
`RAG_TTL_DAYS`; 0 = mantém tudo) — não é base cumulativa ad infinitum. LLM no **OpenRouter**
(`OPENROUTER_API_KEY` + `LLM_NAME`); embeddings locais via `fastembed`. **Custo (--persist):** o
caro é a ingestão (LLM extrai entidades por chunk); gleaning OFF por padrão, e `--extract-model` /
`LLM_EXTRACT_NAME` roteia a extração a um modelo barato (via `role_llm_configs`), `RAG_CHUNK_TOKENS`
ajusta o chunk. Código em `src/viewlyt/rag.py` (puro: preparação dos documentos; I/O lazy:
chat=`openai`, `--persist`=LightRAG/fastembed).

## Estrutura

- `src/viewlyt/htmltext.py` — funções de texto **puras, só com stdlib** (HTML→texto, slug,
  data relativa, flatten, `convert_batch`, `format_comment_lines`, `format_transcript`,
  `format_related`, `format_unified`/`join_unified`, `group_consecutive_comments`). Mantenha sem
  dependências: roda dentro de threads/subinterpretadores, então NUNCA pode importar Selenium.
  `cli.format_comment_lines` é um wrapper fino que injeta o conversor com ThreadPool — saída
  idêntica (travada por teste).
- `src/viewlyt/driver.py` — construtor do Chrome headless com stealth.
- `src/viewlyt/scraper.py` — parsing de URL, bypass de consentimento/bot, coleta em duas fases,
  transcrição, vídeos relacionados (`collect_related`, da barra lateral `#secondary`).
- `src/viewlyt/cli.py` — argparse, orquestração, pool de instâncias, conversão paralela, escrita do arquivo.
- `src/viewlyt/api.py` — API de biblioteca: 1 vídeo (`scrape_video`) e batch com navegador
  reutilizado (`Session` context manager, `scrape_videos` com pool), mais os formatadores no
  `ScrapeResult` (`comment_lines`/`transcript_lines`/`related_lines`/`write`). Tudo sobre o helper
  compartilhado `_scrape_url` (driver já construído/primed); depende só de `driver`/`scraper`/
  `htmltext`, **nunca** de `cli`. O `__init__` re-exporta a API (lazy p/ Selenium, eager p/ puras).
- `src/viewlyt/rag.py` — subsistema **opt-in** de análise por IA (comando `viewlyt-ask`). Parte
  **pura** (preparação dos `out/*.md` em documentos auto-descritivos: `parse_out_filename`,
  `comment_metrics`, `build_document`, `prepare_documents`; expiração `_expired_doc_ids`) + duas
  engines com **import lazy**: **chat efêmero** padrão (`chat`/`chat_repl`, só `openai`, extra `ask`)
  e **`--persist`** LightRAG com janela deslizante (`build_rag`/`ingest`/`ask`/`analyze`/`purge_expired`,
  extra `rag`). Espelha o `live/llm.py` no diferimento de deps; não depende de `cli`/`scraper`.
  Testes em `tests/test_rag.py`.
- `tests/test_units.py` — testes das funções puras (roda também standalone via `python`).
- `tests/conftest.py` — fakes/helpers pytest-only (FakeDriver, builders de registros, `cli_run`, marca e2e).
- `tests/test_integration.py` — integração sem navegador (monkeypatch do boundary Selenium em `cli`/`api`).
- `tests/test_smoke.py` — smoke da CLI por subprocess (`--version`/`--help`/exit codes/entry point).
- `tests/test_e2e.py` — e2e real (Chrome + rede), **opt-in** via `VIEWLYT_E2E=1` (pulado por padrão).
- `src/viewlyt/live/` — subpacote **opt-in** do modo live (real-time, LLM); testes em `tests/test_live_*.py`. Veja @how-to.md.
- `out/` — entregáveis (no `.gitignore`).

## Convenções

- Requer Python >= 3.11 (testado em 3.11–3.14); o desenvolvimento usa 3.14, fixado em `.python-version`.
- Todas as chamadas ao Selenium/WebDriver são single-thread por driver (WebDriver não é
  thread-safe). O paralelismo entre vídeos vem de um **pool de instâncias do Chrome**:
  cada worker tem o **seu próprio** driver (reutilizado entre vídeos) e há até `--jobs`
  workers. Nunca compartilhe um driver entre threads. Falhas são isoladas por vídeo e a
  sessão é recriada se ficar inválida; com vários vídeos, desligue as barras `tqdm` internas
  (`progress=False`) e mostre só a barra geral.
- O único trabalho de CPU paralelizado é o `html_to_text`, via **`ThreadPoolExecutor` em lotes**.
  NÃO troque por `InterpreterPoolExecutor`/`ProcessPoolExecutor` — medido como mais lento
  para muitos parses minúsculos limitados pelo GIL; a etapa é irrisória perto do Selenium.
- Extraia o texto do comentário do **innerHTML** de `#content-text` (nunca `element.text`,
  que perde o `alt` dos emojis); likes de `#vote-count-middle`; data de `#published-time-text`.
  Cada campo é lido por uma **lista ordenada de seletores** (`*_SELECTORS`) via
  `_first_text`/`_first_inner_html` (o primeiro acerto não-vazio vence), para sobreviver
  a renomeações de DOM do YouTube; os nomes escalares legados continuam apontando para o 1º item.
- Formato de saída: um bloco por comentário de primeiro nível, blocos separados por linha em branco:
  - comentário: `@user [N likes, yyyy-mm-dd]: mensagem`
  - resposta:   `    ↳ (in reply to @parent) @author [N likes, yyyy-mm-dd]: mensagem`
  - mensagens achatadas em uma única linha; datas são **aproximadas** (vêm do tempo relativo do YouTube).
  - **Fusão (padrão):** comentários de primeiro nível consecutivos do MESMO autor real
    (≠ `""`/`unknown`) são fundidos em um bloco (likes/data do 1º, textos concatenados, todas
    as respostas mantidas); duplicatas exatas (mesmo autor + texto) são descartadas. É uma etapa
    pura em `htmltext.group_consecutive_comments`, aplicada em `format_comment_lines`; desative
    com `--no-merge-comments` (alias `--prevent-comment-group`).
- **Relacionados (`-r N`):** lê a barra lateral (`#secondary`, renderer `yt-lockup-view-model` — o
  YouTube aposentou `ytd-compact-video-renderer`; Shorts usam outra tag, então são ignorados de
  graça). Expõe **views, não likes** (likes só na página de cada vídeo). Roda DEPOIS dos comentários
  e ANTES da transcrição (o painel de transcrição assume o `#secondary` e cobriria os lockups).
  `collect_related` nunca levanta (retorna `[]`, como `fetch_transcript`); `format_related` (puro)
  gera `N. [<views>. <título>](<url>)`. O modo espelha `-t`: `-r N` sozinho = só relacionados;
  combine com `-c`/`-t`. **Seletores nunca podem usar `class`-substring** (`[class*=…]`) — o teste
  `test_transcript_timestamp_exact_token` proíbe em todo o `scraper.py`; use tokens exatos.
- **Default de seleção (`resolve_modes`):** sem nenhum seletor, coleta a **transcrição** (só). `-c`
  = comentários; `-c -t` = ambos; `-t`/`--transcript-only` = transcrição; `-r N` = relacionados.
- **`--no-ts`:** remove o prefixo de timestamp `[m:ss]`/`[mm:ss]` das linhas da transcrição (puro
  `htmltext.strip_timestamps`; `h:mm:ss` é mantido — casa o regex exato `\[\d?\d:\d\d\] `).
- **`--copy`:** além de escrever, põe o output completo no clipboard (doc unificado, ou o conteúdo do
  arquivo produzido — verbatim para 1 produto) via `pbcopy`/`clip`/`xclip`/`xsel` (`cli._copy_to_clipboard`).
- **Unificação (`-u`/`--unify`/`--unify-all`):** unem os produtos num arquivo. `--unify` (alias `-u`) = 1 por vídeo
  (`<slug>-<id>.unified.md`); `--unify-all` = 1 global (`out/unified-all.md`), mutuamente
  exclusivos. **Sozinhos** (sem `-c`/`-t`/`--transcript-only`) coletam TUDO (comments + transcript
  + 20 related); `-r N` só ajusta o count (não é seletor aqui); com `-c`/`-t` unem só aqueles. A
  ENUMERAÇÃO das seções vive em UM lugar — `ScrapeResult._sections` (api) e o mesmo trio no
  `run_batch` —, e `htmltext.format_unified(title, [(header, lines)])` é **agnóstico de produto**
  (seção vazia é pulada), então um produto novo entra de graça. `ScrapeResult.write(unify=True)` e
  `unified_lines()` dão a paridade na lib; `join_unified` concatena vários vídeos (`--unify-all`).

## Git / commits

- **NÃO adicione trailers `Co-Authored-By` nem qualquer coautoria** nas mensagens de commit.
- Faça commits em blocos pequenos e lógicos, com mensagens no estilo conventional
  (`feat(scraper): …`, `chore: …`, `docs: …`).
- Antes de commitar, rode `uv run ruff format && uv run ruff check && uv run pytest`
  (ou instale o `pre-commit`, que faz isso sozinho).
- **Nunca commite** a saída coletada (`out/` e os `.md` de saída — contêm nomes de usuário/PII), segredos
  ou credenciais (`.env`, `*.pem`, `*.key`, …), `.venv/`, nem perfis de navegador de
  `--user-data-dir` (guardam cookies/sessões). O `.gitignore` já garante isso; se adicionar
  um perfil persistente, mantenha-o fora do repo ou em um caminho coberto pelas regras de ignore.

## Manutenção

- **SEMPRE** remova arquivos residuais/temporários após rodar testes ou coletas — ex.
  `out_test/`, `__pycache__/`, `*.pyc`, `debug_*`, `*.crdownload` e quaisquer scripts temporários.
  Nunca apague os entregáveis em `out/` nem nada em `src/`.
- Prefira escrever execuções de validação descartáveis em `-o out_test`, para serem fáceis de apagar.
