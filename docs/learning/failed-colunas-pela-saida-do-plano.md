---
name: Descobrir colunas usadas pela saída dos nós do plano
description: a lista Output de um nó de varredura no EXPLAIN VERBOSE não diz que colunas a consulta usa, porque o planejador pode listar todas
type: project
category: failed
date: 2026-11-17
---
Não dá para provar pelo plano que uma coluna protegida não foi usada.

**Problema/Contexto:** a ideia era recusar quando `Output` do nó de varredura de uma tabela protegida contivesse a coluna negada. Em junções o planejador costuma emitir a linha física inteira (todas as colunas), mesmo que a consulta só use uma: a regra recusaria consultas corretas.

**O que funciona no lugar:** combinar verificações que cada uma pega um caminho: nome da coluna em qualquer ponto do texto, `*`, lista de apelidos de coluna, referência à linha inteira (essa sim aparece no plano como `apelido.*`), views que dependem da coluna (`pg_depend` com `refobjsubid`) e mascaramento pela origem da coluna do resultado.

**Como aplicar:** `runner._enforce_protected_columns` e `PlanSummary.uses_whole_row`. O limite está documentado em `docs/security.md`: a barreira definitiva é privilégio por coluna no banco.

**Evidência:** `GUARDED_OK` (junção com `vault.files` permitida) e `GUARDED_REFUSED` em `tests/integration/test_guard.py`.
