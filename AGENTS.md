# viewlyt

CLI que coleta os comentários de um vídeo do YouTube (com likes, datas e respostas)
em `out/<title-slug>-<video_id>.txt`, usando Selenium + Google Chrome headless,
gerenciado com `uv`. Veja @README.md para o uso completo.

## Comandos

```bash
uv sync                                             # cria o ambiente e instala as deps (Python >= 3.11; dev em 3.14)
uv run viewlyt '<url-do-youtube>'                # coleta (headless por padrão) -> out/
uv run viewlyt '<url1>' '<url2>'                 # vários vídeos (pool de instâncias reutilizadas)
uv run viewlyt --from-file urls.txt -j 4         # de um .txt/.csv, 4 navegadores em paralelo
uv run viewlyt --limit 150 --max-replies 15 '<url>'
uv run viewlyt -c -t '<url>'                     # comentários + transcrição -> *.transcript.txt
uv run viewlyt -t '<url>'                        # só a transcrição (== --transcript-only)
uv run viewlyt --transcript-only '<url>'         # só a transcrição (alias de -t sem -c)
uv run viewlyt --no-merge-comments '<url>'       # não funde comentários consecutivos do mesmo autor
uv run viewlyt --headed '<url>'                  # navegador visível (melhor contra o bot wall)

uv run pytest                                       # testes (sem navegador)
uv run ruff check --fix                             # lint
uv run ruff format                                  # formatação
uv run pre-commit install                           # roda ruff + pytest a cada commit
```

`uv sync` instala também o dependency-group `dev` (ruff, pytest, pre-commit).
Os testes também rodam sem pytest via `uv run python tests/test_units.py`.

## Estrutura

- `src/viewlyt/htmltext.py` — funções de texto **puras, só com stdlib** (HTML→texto, slug,
  data relativa, flatten, `convert_batch`, `format_transcript`). Mantenha sem dependências: roda
  dentro de threads/subinterpretadores, então NUNCA pode importar Selenium.
- `src/viewlyt/driver.py` — construtor do Chrome headless com stealth.
- `src/viewlyt/scraper.py` — parsing de URL, bypass de consentimento/bot, coleta em duas fases, transcrição.
- `src/viewlyt/cli.py` — argparse, orquestração, pool de instâncias, conversão paralela, escrita do arquivo.
- `src/viewlyt/api.py` — API de biblioteca (`scrape_video`/`ScrapeResult`/`Comment`); depende só de
  `driver`/`scraper`/`htmltext`, **nunca** de `cli`. O `__init__` re-exporta a API pública.
- `tests/test_units.py` — testes das funções puras.
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

## Git / commits

- **NÃO adicione trailers `Co-Authored-By` nem qualquer coautoria** nas mensagens de commit.
- Faça commits em blocos pequenos e lógicos, com mensagens no estilo conventional
  (`feat(scraper): …`, `chore: …`, `docs: …`).
- Antes de commitar, rode `uv run ruff format && uv run ruff check && uv run pytest`
  (ou instale o `pre-commit`, que faz isso sozinho).
- **Nunca commite** a saída coletada (`out/`, `*.txt` — contêm nomes de usuário/PII), segredos
  ou credenciais (`.env`, `*.pem`, `*.key`, …), `.venv/`, nem perfis de navegador de
  `--user-data-dir` (guardam cookies/sessões). O `.gitignore` já garante isso; se adicionar
  um perfil persistente, mantenha-o fora do repo ou em um caminho coberto pelas regras de ignore.

## Manutenção

- **SEMPRE** remova arquivos residuais/temporários após rodar testes ou coletas — ex.
  `out_test/`, `__pycache__/`, `*.pyc`, `debug_*`, `*.crdownload` e quaisquer scripts temporários.
  Nunca apague os entregáveis em `out/` nem nada em `src/`.
- Prefira escrever execuções de validação descartáveis em `-o out_test`, para serem fáceis de apagar.
