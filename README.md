# ytcomments

Coleta os comentários de um ou vários vídeos do YouTube em
`out/<title-slug>-<video_id>.txt` (texto puro, sem tags HTML) usando **Selenium**
+ **Google Chrome** headless, gerenciado com [`uv`](https://github.com/astral-sh/uv).

Abre a página do vídeo e trabalha em duas fases (com barras de progresso `tqdm`):

1. **Carga** — rola até o fim repetidamente (até **25** passos de rolagem) para
   carregar de forma preguiçosa até **100 comentários de primeiro nível** (o
   principal ativo do projeto), ou todos, se forem menos.
2. **Expansão & coleta** — percorre cada thread uma vez: rola até ele, clica em
   **"Read more"** para destruncar o texto, expande as **respostas** com um clique
   confiável (até **10 por comentário** por padrão, configurável) e registra cada
   comentário/resposta com seu **autor**, **número de likes** e **data**.

Os fragmentos de HTML são convertidos em texto puro com um `ThreadPoolExecutor`
em lotes (o `alt` de emojis/emotes e o texto dos links são preservados), e o
resultado é escrito agrupado em blocos — um comentário seguido de suas respostas,
blocos separados por uma linha em branco.

## Requisitos

- `uv` (instala/gerencia o próprio **Python 3.14** — veja `.python-version`)
- Google Chrome instalado em `/usr/bin/google-chrome` (o Selenium Manager baixa
  automaticamente o ChromeDriver compatível — nada para instalar manualmente)

## Instalação

```bash
uv sync
```

## Uso

```bash
# Padrão: headless. Escreve out/<title-slug>-dQw4w9WgXcQ.txt
uv run ytcomments 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'

# Aceita também youtu.be, /shorts/, /embed/ e o id puro:
uv run ytcomments 'https://youtu.be/dQw4w9WgXcQ'

# Navegador visível (mais confiável contra o bot wall):
uv run ytcomments --headed 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'

# Coleta no máximo 50 comentários e ignora respostas (bem mais rápido):
uv run ytcomments --limit 50 --no-replies 'https://youtu.be/dQw4w9WgXcQ'

# Mantém até 25 respostas por comentário:
uv run ytcomments --max-replies 25 'https://youtu.be/dQw4w9WgXcQ'

# Escreve em outro diretório:
uv run ytcomments -o ./dump 'https://youtu.be/dQw4w9WgXcQ'

# Vários vídeos de uma vez (pool de instâncias reutilizadas):
uv run ytcomments '<url1>' '<url2>' '<url3>'

# A partir de um arquivo .txt (uma URL por linha) ou .csv (qualquer coluna):
uv run ytcomments --from-file urls.txt
uv run ytcomments videos.csv -j 4          # 4 navegadores em paralelo
```

### Opções

| Flag | Padrão | Descrição |
|------|--------|-----------|
| `inputs…` | — | uma ou mais URLs/ids e/ou caminhos de `.txt`/`.csv` (posicional) |
| `-f, --from-file PATH` | — | arquivo com URLs/ids (`.txt` uma por linha, `.csv` qualquer coluna); repetível |
| `-j, --jobs N` | `min(4, nº vídeos)` | nº de navegadores concorrentes (instâncias reutilizadas) |
| `--limit N` | `100` | Meta de comentários de primeiro nível a coletar (ou todos, se menos) |
| `--max-viewports N` | `25` | Orçamento de rolagem (nº de passos de rolar-até-o-fim) |
| `--no-replies` | off | Não expande/coleta respostas (mais rápido) |
| `--max-replies N` | `10` | Máximo de respostas por comentário (`0` desativa) |
| `--headed` | off | Usa um navegador visível em vez de headless |
| `--no-fallback` | off | Não tenta de novo em modo visível ao detectar bloqueio |
| `--user-data-dir DIR` | — | Perfil persistente do Chrome (use um já logado para furar o bot wall) |
| `-o, --out-dir DIR` | `out` | Diretório para `<title-slug>-<video_id>.txt` |
| `-q, --quiet` | off | Só loga avisos/erros |

## Vários vídeos (modo batch)

Você pode passar várias URLs e/ou arquivos. As URLs são deduplicadas por id de
vídeo e processadas por um **pool limitado de instâncias do Chrome reutilizadas**:
cada worker mantém **um** navegador e processa vários vídeos em sequência (amortiza
o custo de abrir o Chrome), com até `--jobs` navegadores em paralelo (padrão
`min(4, nº de vídeos)`). Como o trabalho é I/O-bound, isso acelera bastante.

- Falhas são isoladas por vídeo (um vídeo com erro não derruba o lote); uma sessão
  problemática é recriada automaticamente.
- Com **um** vídeo aparecem as barras detalhadas por fase; com **vários**, aparece
  uma barra geral de "vídeos" e um resumo final por vídeo.
- Cada vídeo gera o seu próprio `out/<title-slug>-<video_id>.txt`.

> Cada instância do Chrome consome memória (~300–500 MB). Ajuste `--jobs` conforme a RAM disponível.

## Driblando bloqueios do YouTube/Google

O coletor aplica várias camadas para funcionar numa máquina nova:

1. **Cookies de consentimento** — `SOCS`/`CONSENT` são definidos antes de navegar,
   então o aviso "Antes de continuar no YouTube" é pulado em perfis novos. Um
   clique no botão de consentimento ciente do idioma (Accept all / Aceitar tudo)
   fica como fallback.
2. **Chrome stealth** — user agent realista (não-headless), um `--window-size` real
   (obrigatório, senão os comentários nunca carregam em headless),
   `--disable-blink-features=AutomationControlled`, `excludeSwitches` e um script
   CDP que esconde `navigator.webdriver` e ajusta plugins/idiomas.
3. **Fallback automático para modo visível** — se um bloqueio de consentimento/bot
   ainda for detectado em headless, a execução é repetida automaticamente com um
   navegador visível.

Se um IP sinalizado/de datacenter ainda cair no muro *"Faça login para confirmar
que não é um robô"*, passe `--user-data-dir` apontando para um perfil do Chrome
que já tenha feito login no YouTube — é o bypass mais confiável.

## Formato de saída

`out/<title-slug>-<video_id>.txt` agrupa cada comentário com suas respostas num
**bloco**, blocos separados por uma linha em branco:

```
@user [842 likes, 2026-06-04]: texto do comentário aqui
    ↳ (in reply to @user) @other [4 likes, 2026-06-03]: uma resposta a esse comentário
    ↳ (in reply to @user) @third [0 likes, 2026-06-03]: outra resposta

@nextuser [42 likes, 2026-06-01]: o próximo comentário de primeiro nível

@third_user [7 likes, 2026-05-30]: um comentário sem respostas
```

- A mensagem é achatada em uma única linha (quebras internas viram espaços).
- Emotes/emojis personalizados são mantidos pelo seu texto `alt` (ex.: `:smile:` ou o caractere do emoji).
- As respostas são indentadas como `    ↳ (in reply to @parent) @author …`, deixando
  o pai sempre explícito, e uma linha em branco separa cada bloco de primeiro nível.
- O número de likes é o do próprio YouTube (ex.: `842`, `1.2K`); `0` quando oculto/inexistente.
- A data é **aproximada**: o YouTube só expõe um tempo relativo ("2 days ago"), que é
  convertido para `yyyy-mm-dd` em relação à data da execução (meses≈30d, anos≈365d).
  Autores que não resolvem aparecem como `unknown`.
- O slug do nome do arquivo é o título do vídeo, normalizado em NFKD com acentos
  removidos (títulos em português viram ASCII), em minúsculas e com hífens.

## Organização

```
pyproject.toml            projeto uv + entry point de console-script
src/ytcomments/
  cli.py                  argparse, coleta de URLs/arquivos, pool de instâncias, formatação, saída
  driver.py               construtor do WebDriver Chrome com stealth (timeout de 10s)
  scraper.py              parsing de URL, bypass de consentimento, carga/expansão/coleta em duas fases
  htmltext.py             HTML→texto, data relativa, slug, flatten (puro, testado)
tests/test_units.py       testes sem navegador para as funções puras
```

## Concorrência

A coleta é **limitada por I/O do Selenium** (rolar/clicar/rede), que é
single-thread por necessidade — uma instância de WebDriver não é thread-safe. O
único trabalho paralelizável é a conversão pura `html_to_text`, que é minúscula
perto da fase do Selenium.

Para essa etapa usa-se um `ThreadPoolExecutor` em lotes. Foi uma escolha medida,
não o reflexo padrão — medindo `html_to_text` sobre HTML realista de comentários:

| abordagem | 300 fragmentos | 1600 fragmentos |
|---|---|---|
| laço simples | ~60 ms | ~315 ms |
| thread pool (em lotes) | ~99 ms | ~475 ms |
| `InterpreterPoolExecutor` (PEP 734) | ~220 ms | ~340 ms |

Para muitos parses minúsculos limitados pelo GIL, subinterpretadores/processos
adicionam mais custo de inicialização+pickling do que economizam, então são a
ferramenta errada aqui. O thread pool é mantido porque (a) atende ao requisito de
"usar threads" do projeto, (b) seu overhead é irrisório perto dos minutos de
Selenium e (c) num interpretador **free-threaded** ele paraleliza de verdade:

```bash
uv python install 3.14t      # CPython free-threaded
uv run --python 3.14t ytcomments '<url>'
```

## Notas / limitações

- Comentários de primeiro nível miram o `--limit` (100 por padrão); as respostas são
  limitadas pelo `--max-replies` (10 por padrão) e expandidas em um nível (as threads
  de resposta do YouTube são planas).
- As datas dos comentários são aproximadas a partir dos tempos relativos do YouTube (veja acima).
- Um IP residencial e um perfil logado melhoram muito a confiabilidade.
```
