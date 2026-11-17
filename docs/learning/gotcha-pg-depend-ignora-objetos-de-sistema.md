---
name: pg_depend não registra dependência de objetos de sistema
description: uma view que chama pg_sleep ou lê pg_authid não tem linha em pg_depend para isso; só objetos criados por usuários e extensões aparecem
type: project
category: gotcha
date: 2026-11-17
---
Para saber o que uma view pode chamar, o projeto lê o texto de `pg_get_viewdef` e passa pelo analisador léxico; `pg_depend` serve só para achar as views e tabelas de usuário por baixo.

**Problema/Contexto:** o PostgreSQL não grava em `pg_depend` dependências de objetos "fixados" (os do sistema). Uma view com `pg_sleep(0)` no corpo não aparece ligada à função.

**Por que importa:** confiar só em `pg_depend` deixaria passar funções perigosas embutidas, que são justamente as piores.

**Como aplicar:** `vetting._PROBE_SQL` usa `pg_depend` + `pg_rewrite` para descer nas views de usuário e devolve a definição de cada uma; `_scan_text` extrai os nomes. Dependência de coluna (`refobjsubid`) é confiável para tabelas de usuário e é usada para achar views que expõem coluna protegida.

**Evidência:** casos `trap.view_with_sleep` e `trap.view_over_view` em `REFUSED`; `runner._TAINTED_VIEWS_SQL`.
