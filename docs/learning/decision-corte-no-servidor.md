---
name: Linhas e valores cortados no servidor
description: a consulta final roda como SELECT left(c::text, N) ... FROM (consulta) LIMIT n, para limitar linhas e tamanho de cada valor antes de sair do banco
type: project
category: decision
date: 2026-11-17
---
O limite de linhas e o corte de valores longos acontecem dentro do PostgreSQL, não depois de receber o resultado.

**Problema/Contexto:** `SELECT repeat('x', 500000000)` devolve 500 MB sem erro, e uma tabela com colunas `bytea` grandes faz o mesmo com poucas linhas. Cortar no cliente já é tarde: o servidor montou, a rede transportou e o processo recebeu.

**Alternativas descartadas:** cursor no servidor ou leitura linha a linha limitam linhas, não o tamanho de um valor.

**Como aplicar:** `runner._final_statement` usa uma lista de apelidos de coluna (`AS _pgharbor(c0, c1, ...)`) para lidar com nomes repetidos. Os tipos e a origem de cada coluna vêm de uma descrição prévia com `LIMIT 0`. Ferramenta que lê dados de usuário usa `run_query`, nunca `db.rows`.

**Evidência:** `tests/integration/test_guard.py::test_long_values_are_clipped_on_the_server`.
