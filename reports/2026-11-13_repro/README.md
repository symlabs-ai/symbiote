# Scripts de reprodução — relatório de 2026-11-13

Material de referência do `reports/2026-11-13_relatorio-melhorias.md`. Cada script reproduz um ou mais achados com LLM falso, sem rede. Servem de ponto de partida para os testes de regressão descritos em `reports/2026-11-13_plano-implementacao.md`; não são testes e o pytest não os coleta.

## Como executar

Vários scripts gravam bancos SQLite e diretórios de skills ao lado de si mesmos. Copie a pasta para um diretório temporário e rode de lá, nunca de dentro do repo:

```bash
REPO=/caminho/do/symbiote
TMP=$(mktemp -d) && cp -r "$REPO/reports/2026-11-13_repro" "$TMP/repro" && cd "$TMP/repro/execucao"
PYTHONDONTWRITEBYTECODE=1 "$REPO/.venv/bin/python" t09_max_iters.py
```

Os scripts de `execucao/` e `memoria/` importam o `common.py` da própria pasta, então rode-os com o diretório atual na pasta deles.

## Conteúdo

| Pasta | O que cobre | Itens do relatório |
|---|---|---|
| `verify_mem.py`, `verify_exec.py`, `verify_tools.py` | Verificação independente dos achados mais graves | S-1, S-3, S-7, E-1, E-2, E-5 a E-8, M-1, M-10 |
| `api/` | API HTTP: `live1.py` e `live2.py` exigem um servidor em `127.0.0.1:8008` e recebem o caminho de um arquivo com a chave admin; `close_path.py` e `probe_inicial.py` rodam in-process; `console_escape.html` abre no Chromium (`--headless --dump-dom`) | S-2, S-6, S-10, A-1, A-2, M-2, M-3, E-4, S-9 |
| `execucao/` | Runners, parser, compactação, hooks, bus | E-1 a E-12 |
| `memoria/` | Memória, reflexão, dream, skills, harness, índices | S-1, S-4, M-1 a M-9, E-3, P-1 |
| `tools/` | HTTP tools, SSRF, discovery, browser, CLI, docs, SDK | S-3, S-5, S-7, S-8, T-1 a T-7, C-1, C-2, D |

Os nomes dos arquivos dentro de `execucao/` (`t04_autocompact.py`, `t02_trace_race.py`, ...) e `memoria/` (`r1_isolation.py`, `r5_skills.py`, ...) são os citados no relatório.
