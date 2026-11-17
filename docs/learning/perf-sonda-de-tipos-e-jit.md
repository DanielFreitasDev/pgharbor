---
name: Sonda do catálogo: LATERAL fora de ordem e JIT
description: a consulta de conferência saltou de 10 ms para 200 a 600 ms por dois motivos independentes; os dois têm correção na própria consulta e na abertura da transação
type: project
category: performance
date: 2026-11-17
---
A sonda (`_PROBE_SQL`) roda em toda chamada; qualquer coisa cara nela multiplica o tempo da suíte e de cada consulta do agente.

**Problema/Contexto:** ao incluir os tipos na sonda, a conferência passou de ~10 ms para 190 ms (9.6) e 300 a 600 ms (18). A suíte completa, de 4 minutos, não terminava em 10.

**Causas medidas:**
1. O planejador calculava a subconsulta `LATERAL` dos ganchos para **todos** os tipos do banco (474) e só depois juntava com os 2 alcançados, porque a subconsulta dependia de `pg_type` e a estimativa de linhas de CTE recursiva é grosseira. Correção: fazer a `LATERAL` depender da linha alcançada (`FROM (SELECT ... FROM reach OFFSET 0) r`), e cada ramo do passo recursivo partir de `r.oid`. Resultado na 9.6: 140 ms → 3 ms.
2. Na 12+, o custo estimado passava de `jit_above_cost` e o servidor gastava ~250 ms compilando uma consulta de 3 ms. Correção: `SET LOCAL jit = off` na abertura da transação (11+) e `Db.restore_jit(custo)` antes de rodar a instrução do usuário quando o custo dela alcança o limite do servidor.

**Como aplicar:** ao mexer na sonda, medir por seção (cada chave do `json_build_object` separada) em 9.6 e 18, e olhar `loops=` no `EXPLAIN ANALYZE`. Um `loops` igual ao total de linhas de um catálogo é este problema.

**Evidência:** medição de 17/11/2026 nos containers de teste: seção `types` 140,6 ms (9.6) e 230,5 ms (18) antes; 3,0 ms (9.6) depois da correção 1; conferência inteira de 6 a 20 ms nas duas versões depois da correção 2. Teste `test_own_queries_skip_jit_and_expensive_statements_get_it_back` em `tests/integration/test_core.py`.
