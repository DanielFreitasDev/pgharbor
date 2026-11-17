---
name: Transação somente leitura não é fronteira de segurança
description: BEGIN READ ONLY não impede funções com efeito colateral, SET, COPY PROGRAM, LOCK nem a troca do próprio modo
type: project
category: gotcha
date: 2026-11-17
---
`BEGIN TRANSACTION READ ONLY` barra INSERT, UPDATE, DELETE e DDL. Não barra o resto, e por isso o projeto tem as outras camadas.

**Problema/Contexto:** comprovado em 9.6, 16 e 18: dentro da transação continuam funcionando `pg_terminate_backend`, `pg_read_file`, `lo_export`, `dblink_exec`, `set_config`, `pg_advisory_lock` (o lock sobrevive ao ROLLBACK), `COPY ... TO PROGRAM`, `LOCK TABLE ... ACCESS EXCLUSIVE`, e `SET transaction_read_only = off` quando é a primeira instrução. Do 9.6 ao 15 nem a escrita em objetos grandes (`lo_*`) é bloqueada.

**Por que importa:** vários desses não exigem superusuário (`pg_sleep`, locks consultivos, `set_config`, `query_to_xml`).

**Como aplicar:** a abertura da transação termina com um `SELECT`, que tira o snapshot; depois dele o PostgreSQL não deixa mais a transação virar leitura e escrita. O restante é recusado por nome de função e pelo embrulho da consulta. A lista completa está em `docs/security.md`.

**Evidência:** tabela em `docs/security.md`; lista `REFUSED` de `tests/integration/test_guard.py` e o teste `test_nothing_changed_after_the_attacks`.
