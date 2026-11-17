---
name: psycopg usa o protocolo simples sem parâmetros
description: cursor.execute(sql) sem parâmetros (ou com lista vazia) usa o protocolo simples, que aceita várias instruções numa mensagem
type: project
category: gotcha
date: 2026-11-17
---
SQL vindo de fora só é executado por `Db.raw`, que sempre acrescenta um parâmetro extra, tipado e não usado pela instrução.

**Problema/Contexto:** no psycopg 3.3, `execute(sql)`, `execute(sql, None)`, `execute(sql, ())` e `execute(sql, [])` usam o protocolo simples. Nele `COMMIT; DROP TABLE x` executa as duas instruções, e o `COMMIT` encerra a transação somente leitura. É a mesma falha do servidor MCP de referência do PostgreSQL. Com qualquer parâmetro, o psycopg usa o protocolo estendido, em que o servidor recusa mais de uma instrução (`cannot insert multiple commands into a prepared statement`).

**Por que importa:** é a diferença entre o servidor garantir "uma instrução" e ninguém garantir.

**Como aplicar:** um inteiro extra no fim da lista de parâmetros é aceito pelo servidor mesmo sem a instrução citar `$n`, porque o tipo vai declarado. Um texto extra não serve: o servidor não consegue deduzir o tipo. `cursor.stream()` também força o protocolo estendido, mas não devolve a descrição quando não há linhas.

**Evidência:** `session.Db.raw` e `_PROTOCOL_SENTINEL`; casos `multiple-statements` em `tests/integration/test_guard.py`, verdes do 9.6 ao 19.
