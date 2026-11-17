---
name: Conversão para text difere da saída do tipo
description: valor::text usa a função de conversão do tipo, que para boolean, inet e char(n) não devolve o mesmo que o psql mostra
type: project
category: gotcha
date: 2026-11-17
---
O resultado de `pg_query` vem de `left(coluna::text, N)`, e para alguns tipos esse texto não é o da função de saída.

**Problema/Contexto:** `true::text` é `true` (a saída normal é `t`); `'10.0.0.1'::inet::text` é `10.0.0.1/32`; `char(n)::text` perde os espaços à direita.

**Por que importa:** a conversão de volta para valores JSON lia `t` e devolvia `false` para todo booleano.

**Como aplicar:** `runner._convert` aceita `true` e `t`, e tira o `/32` (ou `/128`) de endereços `inet`. Tipo novo com tratamento próprio precisa de caso em `test_values_keep_their_types`.

**Evidência:** `tests/integration/test_guard.py::test_values_keep_their_types`.
