# ytcomments

CLI que coleta os comentários de um vídeo do YouTube (com likes, datas e respostas)
em `out/<title-slug>-<video_id>.txt`, usando Selenium + Google Chrome headless,
gerenciado com `uv`. Veja @README.md para o uso completo.

## Comandos

```bash
uv sync                                             # cria o ambiente e instala as deps (Python 3.14)
uv run ytcomments '<url-do-youtube>'                # coleta (headless por padrão) -> out/
uv run ytcomments --limit 100 --max-replies 10 '<url>'
uv run ytcomments --headed '<url>'                  # navegador visível (melhor contra o bot wall)
uv run python tests/test_units.py                   # testes sem navegador (sem dependência de pytest)
```

## Estrutura

- `src/ytcomments/htmltext.py` — funções de texto **puras, só com stdlib** (HTML→texto, slug,
  data relativa, flatten, `convert_batch`). Mantenha sem dependências: roda dentro de
  threads/subinterpretadores, então NUNCA pode importar Selenium.
- `src/ytcomments/driver.py` — construtor do Chrome headless com stealth.
- `src/ytcomments/scraper.py` — parsing de URL, bypass de consentimento/bot, carga + coleta em duas fases.
- `src/ytcomments/cli.py` — argparse, orquestração, conversão paralela, escrita do arquivo.
- `tests/test_units.py` — testes das funções puras.
- `out/` — entregáveis (no `.gitignore`).

## Convenções

- Python 3.14, gerenciado por `uv`; fixado em `.python-version`.
- Todas as chamadas ao Selenium/WebDriver são single-thread (WebDriver não é thread-safe).
  O único trabalho paralelizado é o `html_to_text`, via **`ThreadPoolExecutor` em lotes**.
  NÃO troque por `InterpreterPoolExecutor`/`ProcessPoolExecutor` — medido como mais lento
  para muitos parses minúsculos limitados pelo GIL; a etapa é irrisória perto do Selenium.
- Extraia o texto do comentário do **innerHTML** de `#content-text` (nunca `element.text`,
  que perde o `alt` dos emojis); likes de `#vote-count-middle`; data de `#published-time-text`.
- Formato de saída: um bloco por comentário de primeiro nível, blocos separados por linha em branco:
  - comentário: `@user [N likes, yyyy-mm-dd]: mensagem`
  - resposta:   `    ↳ (in reply to @parent) @author [N likes, yyyy-mm-dd]: mensagem`
  - mensagens achatadas em uma única linha; datas são **aproximadas** (vêm do tempo relativo do YouTube).

## Git / commits

- **NÃO adicione trailers `Co-Authored-By` nem qualquer coautoria** nas mensagens de commit.
- Faça commits em blocos pequenos e lógicos, com mensagens no estilo conventional
  (`feat(scraper): …`, `chore: …`, `docs: …`).
- **Nunca commite** a saída coletada (`out/`, `*.txt` — contêm nomes de usuário/PII), segredos
  ou credenciais (`.env`, `*.pem`, `*.key`, …), `.venv/`, nem perfis de navegador de
  `--user-data-dir` (guardam cookies/sessões). O `.gitignore` já garante isso; se adicionar
  um perfil persistente, mantenha-o fora do repo ou em um caminho coberto pelas regras de ignore.

## Manutenção

- **SEMPRE** remova arquivos residuais/temporários após rodar testes ou coletas — ex.
  `out_test/`, `__pycache__/`, `*.pyc`, `debug_*`, `*.crdownload` e quaisquer scripts temporários.
  Nunca apague os entregáveis em `out/` nem nada em `src/`.
- Prefira escrever execuções de validação descartáveis em `-o out_test`, para serem fáceis de apagar.
