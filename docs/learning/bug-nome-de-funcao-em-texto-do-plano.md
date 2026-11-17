---
name: Nome de função dentro de texto no plano
description: buscar chamadas proibidas no plano (EXPLAIN) com expressão regular casa com constantes de texto e recusa consulta legítima
type: project
category: bug
date: 2026-11-17
---
A segunda conferência de funções, feita sobre as expressões do plano, precisa usar o analisador léxico do projeto, não uma expressão regular.

**Problema/Contexto:** a consulta `SELECT 'a\'' , pg_sleep(1) -- '` é uma única constante de texto (com `standard_conforming_strings = on`). O analisador léxico acertava, mas o plano mostra a constante em `Output`, e a expressão regular achava `pg_sleep(` dentro dela e recusava com `function-denied`.

**Por que importa:** recusa falsa em consultas que só mencionam o nome de uma função num texto, por exemplo ao procurar em código-fonte ou em logs.

**Como aplicar:** qualquer busca por nomes em SQL gerado pelo servidor (plano, definição de view, corpo de função) passa por `lexer.tokenize`. A expressão regular `PLAN_DENY_PATTERN` ficou só para o conteúdo de constantes de texto em corpos de função, onde o texto pode virar SQL dinâmico.

**Evidência:** `runner._denied_calls_in_plan`; caso `"SELECT 'a\\'' , pg_sleep(1) -- still inside the string '"` em `ALLOWED` de `tests/integration/test_guard.py`, que falhava antes da troca.
