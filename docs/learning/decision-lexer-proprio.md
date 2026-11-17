---
name: Analisador léxico próprio em vez de parser de terceiros
description: o projeto não usa pglast nem outro parser em produção; usa um analisador léxico próprio e deixa o servidor decidir o tipo da instrução
type: project
category: decision
date: 2026-11-17
---
O tipo e a quantidade de instruções são decididos pelo PostgreSQL (consulta embrulhada como subconsulta, protocolo estendido). O código do projeto só precisa saber que nomes o texto cita, e isso um analisador léxico fiel resolve.

**Problema/Contexto:** a única biblioteca Python madura com o parser real do PostgreSQL, `pglast`, é GPL-3.0, o que contraria a licença MIT pedida para o projeto. Além disso um parser no cliente tem a gramática de uma versão só, e o projeto atende do 9.6 ao 19.

**Alternativas descartadas:** `sqlglot` e `sqlparse` não são o parser do servidor, e diferença entre validador e servidor é via de contorno conhecida; `postgast` (BSD) é novo e com gramática do 17; embutir a `libpg_query` exigiria compilar e distribuir binários.

**Por que importa:** a segurança não depende do analisador acertar o que a instrução é; depende de ele não perder um nome. Por isso ele relata a mais de propósito e recusa o que não entende.

**Como aplicar:** `pglast` fica só no grupo de dependências `oracle`, para `tests/unit/test_lexer_oracle.py`. Nunca importar em `src/`.

**Evidência:** 240 mil textos aleatórios por execução em `test_lexer_oracle.py`, sem nenhum nome ou chamada visto pelo PostgreSQL e não visto pelo projeto.
