---
name: Catálogos reais de cada versão
description: scripts/dev/catalog.py consulta um retrato dos catálogos, views, funções e parâmetros do PostgreSQL 9.6 ao 19
type: reference
category: reference
date: 2026-11-17
---
Antes de escrever SQL de catálogo, consultar `scripts/dev/catalog.py`; a memória erra versão de coluna.

**Como aplicar:** `scripts/dev/catalog.py rel pg_stat_activity` mostra as colunas e em que versões cada uma existe; `fn` procura funções (com volatilidade); `guc` procura parâmetros. Os dados estão em `scripts/dev/catalogs.json.gz` e foram extraídos de servidores reais (imagens oficiais, com `pg_stat_statements`).

**Limite conhecido:** o retrato do 19 é da RC2. Depois do lançamento final, extrair de novo e rodar `tests/unit/test_functions.py`, que acusa função volátil nova ainda não classificada.

**Evidência:** `tests/unit/test_functions.py::test_every_volatile_builtin_has_been_reviewed` acusou `pg_wal_summary_contents` (17+) durante a construção.
