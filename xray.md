# viewlyt — raio-X do repositório

Documento de referência completo para uma IA que precisa entender, modificar ou
depurar este projeto sem precisar reler o código-fonte. A fonte canônica para
superfícies voláteis (tabela de flags, defaults exatos, versões) continua sendo o
`README.md` e o `--help` da CLI; este documento cobre **mecânicas, invariantes e
decisões não-óbvias**.

---

## 1. Propósito e visão geral

`viewlyt` é uma CLI (e biblioteca) que abre vídeos do YouTube com um Chrome
headless via Selenium e coleta comentários (com likes, datas e respostas) e/ou a
transcrição completa, gravando o resultado em arquivos `.txt` limpos em `out/`.

Aceita múltiplas URLs/ids e arquivos `.txt`/`.csv`; processa em paralelo com um
pool de instâncias do Chrome reutilizadas.

---

## 2. Estrutura de módulos e direção de dependências

```
viewlyt/
├── htmltext.py   ← FOLHA PURA (stdlib only, sem Selenium, sem I/O)
├── driver.py     ← constrói o WebDriver Chrome com stealth
├── scraper.py    ← toda interação Selenium vive aqui
├── api.py        ← biblioteca pública; depende de driver/scraper/htmltext
├── cli.py        ← orquestração, pool, formatação, escrita; depende de tudo
└── __init__.py   ← re-exporta a API pública com lazy import (PEP 562)
```

**Regra rígida de dependências** (violá-la quebra invariantes testados):

- `htmltext.py` NUNCA importa Selenium, `driver`, `scraper`, `api` ou `cli`.
- `api.py` NUNCA importa `cli`.
- `__init__.py` carrega símbolos Selenium de forma LAZY via `__getattr__`
  (PEP 562) — `import viewlyt` é Selenium-free; o teste
  `test_lazy_import_no_selenium` verifica isso em subprocess.

---

## 3. O contrato de registro (record contract)

O canal de comunicação entre `scraper.collect_comments` e `cli`/`api` é uma
**lista plana ordenada de dicts**. Cada comentário de primeiro nível é
imediatamente seguido pelas suas respostas:

```python
# Comentário de primeiro nível
{"kind": "comment", "author": str, "html": str, "likes": str, "date_raw": str}

# Resposta (segue imediatamente o comentário pai)
{"kind": "reply", "author": str, "parent_author": str,
 "html": str, "likes": str, "date_raw": str}
```

**Atenção à renomeação de chave**: o dict intermediário de harvest (produzido por
`_harvest_thread` / `_harvest_thread_fallback`) usa a chave `"date"`. A função
`collect_comments` renomeia para `"date_raw"` ao achatar o resultado. Nunca
confunda as duas.

- `html` é sempre o `innerHTML` bruto (nunca `.text` do Selenium; ver §5).
- `likes` é a string literal do YouTube (e.g. `"842"`, `"1.2K"`); `"0"` quando
  ausente ou oculto.
- `author` pode ser `""` (não resolvido); renderizado como `"unknown"` na saída.
- `parent_author` está presente apenas em registros `kind == "reply"`.

---

## 4. Modelo de concorrência

### 4.1 WebDriver é single-thread por instância

O WebDriver não é thread-safe. **Nunca compartilhe um driver entre threads.**

O paralelismo é realizado por um **pool de instâncias**: `run_batch` cria
`jobs` threads de worker, cada uma com o **seu próprio driver** (reutilizado
entre vídeos). O padrão é `min(4, len(targets))` workers, com máximo de 1 por
vídeo.

### 4.2 O único paralelismo de CPU: `html_to_text` via ThreadPoolExecutor

`_convert_all` em `cli.py` distribui a conversão HTML→texto via
`ThreadPoolExecutor` em lotes de 64 fragmentos, com `min(8, cpu_count())`
workers.

**Esta foi uma escolha deliberada baseada em benchmark** (tabela no README):

| abordagem | 300 fragments | 1600 fragments |
|---|---|---|
| loop simples | ~60 ms | ~315 ms |
| thread pool (em lotes) | ~99 ms | ~475 ms |
| `InterpreterPoolExecutor` | ~220 ms | ~340 ms |

Para muitos parses tiny limitados pelo GIL, subinterpretadores e processos
adicionam overhead de startup/pickling maior que o ganho. **Não "otimize" para
`ProcessPoolExecutor` ou `InterpreterPoolExecutor`** — medido como mais lento.
Em CPython free-threaded (3.14t) o thread pool realmente paraleliza.

---

## 5. Extração de dados do DOM — por que não `.text`

### Texto do comentário: `innerHTML`, nunca `element.text`

`element.text` do Selenium ignora o atributo `alt` de imagens, o que silencia
emojis customizados que o YouTube renderiza como `<img alt=":smile:">`. A
solução é ler o `innerHTML` bruto de `#content-text` e processar com
`htmltext.html_to_text`.

### Autor: `textContent`, nunca `.text`

`.text` retorna apenas texto *visível/renderizado*; o nome de um comentário de
dono do canal pode estar em um badge, ficando off-screen. `textContent` lê o
nome independentemente de visibilidade.

### Funções helper em `scraper.py`

```python
_text(el, css)         # textContent colapsado via get_attribute
_inner_html(el, css)   # innerHTML via get_attribute
_first_text(el, sels)  # primeiro seletor da lista com textContent não-vazio
_first_inner_html(el, sels)  # primeiro seletor com innerHTML não-branco
_likes(comment_el)     # _first_text sobre LIKES_SELECTORS; "0" como fallback
```

---

## 6. Seletores com fallback ordenado

Cada campo usa uma **lista ordenada de seletores CSS**; o primeiro que retornar
valor não-vazio vence. Isso permite sobreviver a renomeações do DOM do YouTube
sem quebrar silenciosamente.

```python
TOP_COMMENT_SELECTORS = (
    "#comment",
    "ytd-comment-view-model#comment",
    "#comment-content",
)
COMMENT_TEXT_SELECTORS = (
    "#content-text",  # canônico
    "yt-attributed-string#content-text",
    "#comment-content #content-text",
    "#content #content-text",
    "yt-formatted-string#content-text",
)
COMMENT_AUTHOR_SELECTORS = (
    "#author-text",
    "a#author-text",
    "#header-author #author-text",
    "#author-comment-badge #author-text",  # caso badge/dono do canal
    "h3 #author-text",
)
LIKES_SELECTORS = (
    "#vote-count-middle",
    "#vote-count-left",
    "[id*=vote-count]",  # fallback last-ditch
)
PUBLISHED_TIME_SELECTORS = (
    "#published-time-text",
    "#published-time-text a",
    "a.yt-simple-endpoint#published-time-text",
    "#header-author #published-time-text",
)
```

Os nomes escalares (e.g. `TOP_COMMENT`, `COMMENT_TEXT`) são aliases para o
primeiro elemento e mantidos por compatibilidade de leitura.

---

## 7. A dualidade de clique (armadilha principal)

Esta é a decisão mais contraintuitiva do código:

| Ação | Tipo de clique | Motivo |
|------|----------------|--------|
| Expandir respostas (`#more-replies`) | **Selenium `.click()` confiável** | Um click JS não dispara o fetch de respostas do YouTube |
| Controles de transcrição | **JS `.click()`** | Um click confiável do Selenium é no-op aqui |
| `Read more` (truncamento) | JS `.click()` | Simples; não precisa do evento nativo |

`_safe_click` usa `.click()` Selenium com fallback para JS se interceptado —
correto para replies. `_js_click` usa apenas JS — correto para transcrição.
**Nunca troque os dois.**

---

## 8. Coleta em duas fases (two-phase harvest)

### Por que duas fases?

A coleta e a expansão brigavam entre si quando feitas no mesmo loop (scroll
carregava novos threads enquanto replies ainda estavam expandindo), gerando
artefatos de linhas duplicadas. A separação elimina isso.

### Fase A — Carregamento (`collect_comments`)

1. Scroll para trazer `ytd-comments#comments` para a viewport.
2. Aguarda o primeiro `ytd-comment-thread-renderer` aparecer (timeout `30s`);
   durante a espera, re-nuge o observer com scrolls incrementais.
3. Loop de `max_viewports=25` iterações de scroll: cada vez espera o count de
   threads crescer (timeout `10s`); após um timeout, tenta um up-nudge + retry;
   3 iterações consecutivas sem crescimento (`stale >= 3`) terminam o loop.
4. Coleta os primeiros `limit=150` threads da página.

### Fase B — Expansão + Harvest (por thread, em ordem)

Para cada thread, na mesma iteração:

1. Clica `#more-replies` (Selenium `.click()`) e aguarda replies aparecerem
   (timeout `6s`; `REPLY_ITEM_ANY` = union de seletores para cobrir layouts
   modernos e legados).
2. **Harvest em round-trip único** via `_harvest_thread` (executa um script JS
   que lê author, innerHTML, likes, date, e replies em um `execute_script`).
3. Se o JS lançar `WebDriverException`, cai no `_harvest_thread_fallback`
   (leitura per-elemento, o caminho lento mas comprovado).
4. Threads com corpo vazio são descartadas.

**Por que interleaved (expand → harvest no mesmo thread)?** Listas longas com
virtualização podem reciclar o DOM de threads anteriores antes que sejam lidos.
Interleaving garante que cada thread seja lido logo após expandido.

---

## 9. Seletores de transcrição e invariante crítico

```python
TRANSCRIPT_SEGMENT = "transcript-segment-view-model, ytd-transcript-segment-renderer"
TRANSCRIPT_TS      = ".ytwTranscriptSegmentViewModelTimestamp, .segment-timestamp"
TRANSCRIPT_SEG_TEXT = "span.ytAttributedStringHost, yt-formatted-string.segment-text, .segment-text"
```

**Invariante**: O seletor de timestamp DEVE ser um token exato de classe CSS
(`.token`, que nunca corresponde a substring). O DOM moderno do YouTube tem um
nó irmão `.ytwTranscriptSegmentViewModelTimestampA11yLabel` com o texto
"30 minutes, 40 seconds". Um seletor de substring `[class*=Timestamp]`
capturaria ambos. O teste `test_transcript_timestamp_exact_token` verifica que
nunca há `[class*=` em `scraper.py`.

### Abertura do painel de transcrição (três tentativas)

1. Botão na seção de transcrição da descrição (`ytd-video-description-transcript-section-renderer > button`).
2. Qualquer controle visível com `/transcri/` no aria-label ou textContent (exceto "Hide/Ocultar").
3. Menu overflow `...more` → rescan.

Se nenhum controle for encontrado: `_open_transcript_panel_direct` dispara o
evento Polymer diretamente (sem click); budget de espera curto (`1.2s`) pois
vídeos sem transcrição (clipes musicais) são o caso mais comum.

`fetch_transcript` **nunca lança exceção** — retorna `[]` em qualquer falha.
Isso garante que um problema na transcrição nunca aborte comentários já coletados
nem recicle um driver do pool.

---

## 10. Stack anti-bot e bypass de consentimento

Em ordem de aplicação:

1. **Cookies de consentimento** (`prime_consent_cookies`): define `SOCS=CAI` e
   `CONSENT=YES+` em `.youtube.com` antes de navegar, eliminando o aviso
   "Before you continue to YouTube" em profiles novos.
2. **User agent realista**: `Mozilla/5.0 (X11; Linux x86_64) ... Chrome/149.0.0.0`
   — sem "HeadlessChrome".
3. **Chrome flags**: `--disable-blink-features=AutomationControlled`,
   `excludeSwitches: ["enable-automation"]`, `useAutomationExtension: false`.
4. **Script CDP stealth** (injetado via `Page.addScriptToEvaluateOnNewDocument`):
   esconde `navigator.webdriver`, define `languages`/`plugins`, preenche
   `window.chrome.runtime`.
5. **`page_load_strategy = "eager"`**: para no DOMContentLoaded (a watch page
   nunca termina de carregar completamente).
6. **`window-size=1920,1080`** obrigatório em modo headless: sem viewport,
   o `IntersectionObserver` do YouTube nunca dispara e os comentários não
   lazy-carregam.
7. **Fallback automático para headed**: se `detect_block` detectar consent/botwall
   no modo headless, o worker reconstrói o driver em modo headed e retenta.
8. **`--user-data-dir`**: profile persistente já logado — bypass mais confiável
   em IPs flagged/datacenter.

`detect_block` retorna `"consent"` se a URL contiver `"consent."`, e `"botwall"`
se o source da página contiver `"sign in to confirm"` ou `"not a bot"`.

---

## 11. Módulo `htmltext.py` — funções puras

**Não tem dependências externas** (só stdlib). É importado dentro de threads e
subinterpretadores — nunca adicione Selenium ou qualquer I/O aqui.

### `html_to_text(html: str) -> str`

`_CommentTextExtractor(HTMLParser)`:
- `<br>` / `<br/>` → `\n`
- `<p>` / `<div>` → `\n` nas bordas
- `<img>` → `alt` (fallback `aria-label`, `shared-tooltip-text`)
- `<a>` → texto interno visível (ignora href)
- Entidades HTML decodificadas por `convert_charrefs=True`
- Em falha do parser: strip de tags via regex (nunca aborta)

### `flatten_inline(text: str) -> str`

Colapsa toda whitespace (incluindo `\n`) em um único espaço. Cada comentário
ocupa exatamente uma linha na saída.

### `group_consecutive_comments(records: list[dict]) -> list[dict]`

A função de merge/dedup. Recebe a lista plana do scraper, retorna nova lista
plana (não muta a entrada).

**Algoritmo**:
1. Divide a lista plana em blocos: cada `kind == "comment"` inicia um bloco;
   `kind == "reply"` acrescenta ao bloco corrente.
2. Caminha os blocos mantendo `kept` e `seen_keys`:
   - Se o autor é real (não `""` / `"unknown"`) E o mesmo que o bloco anterior:
     **merge** — concatena HTML com `<br>`, mantém todas as replies, preserva
     `likes`/`date_raw` do primeiro.
   - Se é um duplicata exata (mesmo autor + mesmo texto renderizado, case-insensitive,
     via `_comment_key`): **descarta** (com suas replies).
   - Autores anônimos (`""`, `"unknown"`) nunca são merged nem deduplicados
     entre si (dois anônimos não são a mesma pessoa).
3. Reconstrói a lista plana: leading orphan replies → comentários com suas replies.

A chave de comparação (`_comment_key`) usa o texto *renderizado* (não o HTML
bruto) para que `<b>x</b>` e `x` contem como iguais.

### `parse_relative_date(text: str, today: date) -> str`

Converte `"2 days ago"` → `"2026-06-04"`. Approximações:
- `month ≈ 30d`, `year ≈ 365d`
- `second/minute/hour → 0 dias` (retorna `today`)
- `"just now"` / `"moment"` → `today`
- `"a/an X"` → `"1 X"`
- `"(edited)"` ignorado
- Texto não parseável → retornado inalterado (sem perda de dados)

### `slugify(text: str, max_len: int = 80) -> str`

NFKD → drop accents → lowercase → `[^a-zA-Z0-9]+` → `-` → trim → cap em
`max_len` (sem trailing hyphen). Retorna `"video"` se nada restar.

### `format_transcript(segments: list[tuple[str, str]]) -> list[str]`

Formata `[(ts, text)]` como `"[ts] text"`. Sem deduplicação (refrões e
`[Music]` repetidos são conteúdo legítimo). Segmentos com texto vazio após
colapso de whitespace são descartados; timestamp é verbatim (só trim de padding).

---

## 12. Módulo `driver.py`

Constrói o `webdriver.Chrome` com todas as opções de stealth (ver §10). Pontos
não-óbvios:

- `_resolve_chrome_binary()`: `$VIEWLYT_CHROME_BINARY` → `/usr/bin/google-chrome`
  → qualquer `chrome`/`chromium` no PATH → `None` (Selenium Manager auto-detecta,
  funciona no macOS/Windows sem configuração).
- `page_load_strategy = "eager"` (DOMContentLoaded) porque a watch page mantém
  sockets abertos indefinidamente.
- `page_load_timeout = 10s` (corresponde a `_DEFAULT_TIMEOUT_NOTE` em `scraper`).
- Se o CDP falhar ao injetar o stealth script, loga debug e segue (nunca lança).

---

## 13. Módulo `scraper.py` — fluxo de navegação

### `extract_video_id(url: str) -> str`

Ordem de tentativa:
1. Se já é um id de 11 chars: retorna.
2. Host `youtu.be`: path segment.
3. Host `youtube.*`: query string `v=`; depois path `/shorts/`, `/embed/`, `/v/`, `/live/`.
4. Last resort: primeiro run de 11 chars alfanumérico + `-` + `_` na URL inteira.

**Limitação conhecida (bloqueada por teste)**: attribution_link com
`/attribution_link?u=%2Fwatch%3Fv%3D...` faz o last resort capturar `"attribution"`
ao invés do id real.

### Fluxo principal de uma URL em `scrape_one` (cli) / `scrape_video` (api)

```
prime_consent_cookies(driver)
safe_get(driver, watch_url)       # para em TimeoutException, segue com DOM parcial
dismiss_consent_dialog(driver)    # best-effort click no botão de consentimento
detect_block(driver)              # raise BlockedError se consent/botwall
get_video_title(driver)           # og:title meta tag; fallback document.title
collect_comments(driver, ...)     # fase A + fase B (ver §8)
collect_related(driver, ...)      # barra lateral #secondary; nunca lança (ver §13b)
fetch_transcript(driver, ...)     # nunca lança (ver §9)
```

**Ordem importa:** `collect_related` roda **depois** dos comentários e **antes**
da transcrição. O painel de transcrição assume o `#secondary` (a coluna que
hospeda os lockups de relacionados), então coletá-los antes de abrir o painel é
obrigatório. `scrape_one`/`scrape_video` retornam, respectivamente, a 5-tupla
`(video_id, title, records, transcript, related)` e um `ScrapeResult` com
`.related`.

### `safe_get`

Captura `TimeoutException` do page load e executa `window.stop()` —
necessário porque a watch page não termina de carregar.

---

## 14. Módulo `cli.py` — orquestração e formato de saída

### `resolve_modes(comments, transcript, transcript_only, related) -> (with_comments, with_transcript, with_related)`

`related` é o **count** (`-r N`; `> 0` liga). Comentários são o default implícito
**só** quando nenhum outro seletor é dado — espelhando `-t`/`-r` sozinhos.

| flags passadas | with_comments | with_transcript | with_related |
|----------------|---------------|-----------------|--------------|
| nenhuma | `True` | `False` | `False` |
| `-c` | `True` | `False` | `False` |
| `-t` | `False` | `True` | `False` |
| `-r N` | `False` | `False` | `True` |
| `-c -t` | `True` | `True` | `False` |
| `-c -r N` | `True` | `False` | `True` |
| `-t -r N` | `False` | `True` | `True` |
| `-c -t -r N` | `True` | `True` | `True` |
| `--transcript-only` | `False` | `True` | `False` |

`--transcript-only` **ganha sobre `-c`**. A regra única: `with_comments =
comments or not (with_transcript or with_related)` (exceto `transcript_only`, que
força `with_comments=False`).

### Formato de saída dos comentários

```
@user [842 likes, 2026-06-04]: mensagem aqui
    ↳ (in reply to @user) @outro [4 likes, 2026-06-03]: resposta
    ↳ (in reply to @user) @terceiro [0 likes, 2026-06-03]: outra resposta

@proximo [42 likes, 2026-06-01]: próximo comentário
```

`REPLY_INDENT = "    ↳ "` (4 espaços + ↳). Blocos separados por linha em branco.
A função `format_comment_lines` conta como top-level qualquer linha não-vazia que
não começa com `REPLY_INDENT`.

### Formato de saída dos relacionados (`format_related`, puro)

```
1. [1.2B views. Título do Vídeo](https://www.youtube.com/watch?v=ID)
2. [20M views. Outro Título](https://www.youtube.com/watch?v=ID2)
```

Lista numerada 1-based. `views` é o texto da sidebar **verbatim** (já inclui a
palavra "views"/"visualizações" — nada é anexado, evitando "views views"). Sem
`views` → `N. [Título](url)`. Itens sem título são pulados sem consumir número.
**Limitação:** título com `]` (ex. `[4K Remaster]`) quebra o link Markdown
sintaticamente — mantido assim de propósito (texto continua legível).

### Arquivo de saída

`out/<slug>-<video_id>.txt` para comentários.
`out/<slug>-<video_id>.transcript.txt` para transcrição.
`out/<slug>-<video_id>.related.txt` para relacionados.

### `run_batch` — pool de workers

- Fila `Queue` de `(video_id, url)`.
- `jobs` threads de worker; cada uma tem seu próprio driver (criado na primeira
  necessidade, reutilizado até erro).
- Falha em um vídeo: grava `error` no summary, faz `driver.quit()`, reseta
  `driver = None` (próximo vídeo recria a sessão).
- Fallback headed: se `BlockedError` + `fallback=True`, rebuilda o driver em
  `headless=False` e retenta **só esse vídeo**.
- Ordena o summary final pela ordem de entrada (not de conclusão).
- `inner_progress=True` só para exatamente 1 vídeo (tqdm interno ativo).

---

## 15. Módulo `api.py` — uso como biblioteca

Tudo gira em torno do helper compartilhado **`_scrape_url(driver, url, *, ...)`**:
faz `safe_get`→`detect_block`→coleta e devolve um `ScrapeResult`, **sem**
construir nem fechar o driver (o chamador é dono do ciclo de vida). Três frentes
o usam:

- **`scrape_video(url, *, comments, transcript, related, ...)`** — constrói/prima/
  fecha o próprio Chrome (`try/finally quit`), 1 vídeo, **sem** fallback (lança
  `BlockedError` em consent/botwall). Nunca escreve arquivos.
- **`Session(*, headless, user_data_dir, fallback)`** — context manager que
  constrói+prima **um** Chrome de forma lazy (no 1º `scrape`) e raspa vários
  vídeos nele (sem cold-start). `scrape(url, ...)` faz fallback headless→headed
  num bloqueio (a menos de `fallback=False`). `close()` é idempotente.
- **`scrape_videos(urls, *, jobs=4, ...)`** — pool de `jobs` workers, cada um com
  sua `Session` reutilizada. Retorna `list[ScrapeResult | None]` **alinhada à
  ordem de entrada** (None por falha, logada — nunca descartada silenciosamente);
  sessão envenenada é reciclada. WebDriver continua single-thread (1 driver por
  worker, nunca compartilhado).

`ScrapeResult` (`dataclass(slots=True)`):
- `.comments` / `.top_level` / `.replies` — objetos `Comment`
- `.related` — objetos `RelatedVideo`
- `.transcript` — `[(timestamp, text)]`
- `._records` — **privado** (`repr=False`): os records crus do scraper (com HTML),
  guardados para `comment_lines`/`write` reusarem o pipeline EXATO da CLI
- `.comment_lines(merge=True)` — corpo idêntico ao `out/<slug>-<id>.txt` da CLI
- `.transcript_lines()` / `.related_lines()` — delegam a `format_transcript`/`format_related`
- `.write(out_dir, merge=True) -> dict[str, Path]` — grava `.txt`/`.transcript.txt`/
  `.related.txt` (só seções não-vazias), retorna `{seção: path}`

`Comment(kind, author, text, likes, date, parent_author)` — note `date` (string
relativa raw, ex. `"2 days ago"`), ao contrário do record do scraper (`date_raw`).
`RelatedVideo(video_id, title, views, url)`.

---

## 16. Módulo `__init__.py` — lazy import PEP 562

```python
_LAZY = {
    "scrape_video": "api",
    "scrape_videos": "api",
    "Session": "api",
    "ScrapeResult": "api",
    "Comment": "api",
    "RelatedVideo": "api",
    "build_driver": "driver",
    "collect_comments": "scraper",
    "collect_related": "scraper",
    "fetch_transcript": "scraper",
    "extract_video_id": "scraper",
    "BlockedError": "scraper",
}
```

`__getattr__` importa o módulo no primeiro acesso e faz cache em `globals()`.
Símbolos puros (`html_to_text`, `format_comment_lines`, `group_consecutive_comments`,
`slugify`, etc.) são importados eagerly de `htmltext` (sem custo, sem Selenium).
Os **nav primitives** (`safe_get`, `detect_block`, …) NÃO são expostos de
propósito — `Session` é o seam suportado para dirigir um driver reutilizado.
O teste `test_lazy_import_no_selenium` trava: (a) `import viewlyt` e as puras não
puxam Selenium; (b) `Session`/`scrape_videos` resolvem (typo em `_LAZY` só falha
no acesso).

---

## 17. Suíte de testes

### Camadas

| Arquivo | Tipo | Requer navegador? |
|---------|------|-------------------|
| `tests/test_units.py` | Unitário, funções puras | Não |
| `tests/test_integration.py` | Integração, monkeypatch da boundary Selenium | Não |
| `tests/test_smoke.py` | Smoke CLI por subprocess | Não |
| `tests/test_e2e.py` | E2E real (Chrome + rede) | Sim (opt-in) |

### Como rodar

```bash
uv run pytest                       # tudo exceto e2e
VIEWLYT_E2E=1 uv run pytest -m e2e  # e2e real (precisa do Chrome)
uv run python tests/test_units.py   # unitários standalone sem pytest
```

### Ponto crítico do monkeypatch (test_integration)

As funções de boundary são patchadas nos módulos **consumidores** (`viewlyt.cli`,
`viewlyt.api`), **não** em `viewlyt.scraper`/`viewlyt.driver`. Isso porque `cli`
e `api` importam via `from .scraper import ...` — o binding já aconteceu.
Patchear `viewlyt.scraper.collect_comments` não rebindaria o nome que o código
chama.

### `conftest.py`

- `FakeDriver` — duck-type mínimo; registra chamadas a `quit()`.
- `make_comment` / `make_reply` — builders de registro no formato do scraper.
- `make_scrape_one(table)` — fábrica de `scrape_one` falso (mapeia url → resultado).
- `cli_run(args)` — executa `main(argv)` via subprocess com `PYTHONPATH=src`.
- `E2E` — `pytest.mark.skipif` ativado pela var `VIEWLYT_E2E=1`.

---

## 18. Defaults e constantes numéricas relevantes

| Constante | Valor | Onde |
|-----------|-------|------|
| `limit` | 150 | cli default, api default |
| `max_viewports` | 25 | cli default, api default |
| `max_replies` | 5 | cli default, api default |
| `jobs` | `min(4, len(targets))` | cli |
| `page_load_timeout` | 10s | `build_driver` |
| `first_thread_timeout` | 30s | `collect_comments` |
| `stale >= 3` | 3 iterações sem crescimento | load phase termina |
| reply expand timeout | 6s (inicial) / 5s (continuação) | `_expand_replies` |
| batch size | 64 | `_convert_all` |
| ThreadPool workers | `min(8, cpu_count)` | `_convert_all` |
| Chrome window size | 1920×1080 | `build_driver` (obrigatório em headless) |
| reply max_more_clicks | 20 | `_expand_replies` |
| `related` | 0 (off) | cli/api default (`-r N` liga) |
| related nudge-scroll | 12 iterações / `stale >= 3` | `collect_related` |

---

## 19. Não-commitáveis e PII

O `.gitignore` exclui:
- `out/`, `out_test/`, `*.txt` (exceto `requirements*.txt`) — saída do scraper
  contém nomes de usuário (PII).
- `.env`, `*.pem`, `*.key`, etc. — segredos.
- `chrome-profile*/`, `user-data-dir/` — cookies de sessão.
- `__pycache__/`, `*.pyc`, `.venv/`, `.pytest_cache/`, etc.

Nota: `xray.md` e outros `.md` **não** são ignorados; não commite este arquivo
a menos que explicitamente solicitado.

---

## 20. Workflow de desenvolvimento

```bash
uv sync                        # cria .venv e instala deps + grupo dev
uv run pre-commit install      # instala hooks: ruff format → ruff check → pytest
uv run ruff format             # formatar
uv run ruff check --fix        # lint com auto-fix
uv run pytest                  # suíte completa (sem browser)
```

Ruff: `line-length=100`, regras `E F I W UP B`, `E501` ignorado (formatador trata
comprimento; strings longas não são flagadas).

Commits: convencional (`feat(scraper): ...`), blocos pequenos, sem trailers
`Co-Authored-By`.

---

## 21. Armadilhas e decisões não-óbvias — resumo

1. **Click duality**: replies precisam de `.click()` Selenium; transcrição
   precisa de `.click()` JS. Trocar os dois quebra silenciosamente.
2. **`date_raw` vs `date`**: o harvest interno usa `"date"`, `collect_comments`
   renomeia para `"date_raw"`.
3. **Monkeypatch no consumidor, não na fonte**: para `cli` e `api`, patcheie
   os nomes importados *neles*, não em `scraper`/`driver`.
4. **ThreadPool medido, não padrão**: não "otimize" para processos/subinterpretadores
   — foi medido como mais lento para este workload.
5. **`htmltext.py` sem Selenium**: adicionar qualquer import de Selenium aqui
   quebra threads, subinterpretadores e o lazy-import invariant.
6. **`fetch_transcript` nunca lança**: retorna `[]` em qualquer erro — proposital.
7. **Nenhum seletor com `class`-substring**: o teste `test_transcript_timestamp_exact_token`
   verifica que a sequência `[class*=` não aparece em LUGAR NENHUM do `scraper.py`
   — nem em comentários. Use tokens de classe exatos (`.token`). Vale para os
   seletores de transcrição E de relacionados.
8. **`window-size` obrigatório**: sem viewport o IntersectionObserver não dispara.
9. **Anônimos nunca merged**: `""` e `"unknown"` não são mesclados nem deduplicados.
10. **`-t`/`-r N` sozinhos = só aquilo**: comentários são o default implícito só
    quando nenhum outro seletor é dado (mudança de semântica vs. comportamento antigo).
11. **Relacionados antes da transcrição**: o painel de transcrição assume o
    `#secondary`; `collect_related` deve rodar antes. Renderer atual é
    `yt-lockup-view-model` (o `ytd-compact-video-renderer` foi aposentado); a
    sidebar dá **views, não likes**. `collect_related` nunca lança (`[]`).
12. **`scrape_one` retorna 5-tupla** `(video_id, title, records, transcript, related)` —
    desempacotada em DOIS lugares no worker de `run_batch` (normal + fallback headed);
    mantenha os dois em sincronia.
13. **Formatter de comentário é puro em `htmltext`**: `format_comment_lines` vive em
    `htmltext` com conversor injetável (default `convert_batch`). `cli.format_comment_lines`
    é só um wrapper que injeta o `_convert_all`/ThreadPool. A saída DEVE ser idêntica —
    travada por `test_format_comment_lines_pure_matches_cli`. Não divirja as duas.
14. **`scrape_videos` retorna `list[ScrapeResult | None]`** alinhada à entrada (None por
    falha, logada). NÃO descarte falhas silenciosamente nem mude para retorno desalinhado.
    `_scrape_url` é o seam compartilhado (driver já construído/primed) entre `scrape_video`,
    `Session` e `scrape_videos`.
