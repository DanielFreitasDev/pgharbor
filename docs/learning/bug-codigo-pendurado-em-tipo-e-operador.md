---
name: Código que roda sem ser citado: tipo, conversão, domínio e operador
description: cast com função, CHECK de domínio e operador próprio escrito dentro de view ou função executavam código que a conferência não via
type: project
category: bug
date: 2026-11-17
---
A conferência segue o código pendurado em tipos de usuário e confere operadores em todo texto que lê, não só na instrução.

**Problema/Contexto:** três caminhos executavam função sem que o nome dela aparecesse:
1. `SELECT 1::meu_tipo` roda a função do `CREATE CAST`;
2. `SELECT 1::meu_dominio`, ou passar um valor para um parâmetro do tipo do domínio, roda o `CHECK`;
3. `1 OPERATOR(esquema.===) 2` escrito dentro de uma view, de um corpo de função ou de um `CHECK` roda a função do operador. Os operadores só eram conferidos no texto da instrução.

Nos três, a função podia ler tabela protegida ou pegar lock (reproduzido em 9.6 e 18).

**Por que importa:** são objetos que alguém com `CREATE` já deixou no banco; o agente só precisa usá-los.

**Como aplicar:**
- a sonda devolve, para os tipos alcançados, as funções em linguagem procedural atrás de casts, operadores, classes de operador e faixas, e o texto dos `CHECK` de domínio; tudo entra na fila como chamada direta;
- tipo alcançado = citado pelo nome, coluna de tabela citada (o nome da tabela é o nome do tipo de linha), ou presente na assinatura de função chamada pelo nome;
- código compilado (C) pendurado em tipo não é seguido: é da extensão (`ivfflat_halfvec_support` do pgvector é VOLATILE por omissão e recusaria tudo);
- `_scan_text` devolve também os operadores encontrados.

**Evidência:** seções `types` e `operators` de `_PROBE_SQL` em `src/pgharbor/vetting.py`; `trap.sticky`, `trap.checked`, `trap.takes_checked`, `trap.holder`, `trap.tagged`, `trap.view_with_operator`, `trap.uses_operator*` e `trap.checked_by_operator` em `tests/fixture.py`, com os casos correspondentes em `REFUSED`.
