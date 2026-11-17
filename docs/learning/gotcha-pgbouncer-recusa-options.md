---
name: PgBouncer recusa o parâmetro options na conexão
description: em modo de transação o PgBouncer não aceita options no início da conexão e não mantém estado de sessão entre transações
type: project
category: gotcha
date: 2026-11-17
---
Os ajustes de segurança vão por `SET LOCAL` dentro de cada transação; as opções de início da conexão são só um reforço.

**Problema/Contexto:** com PgBouncer 1.26 em modo de transação, conectar com `options=-c ...` falha com "unsupported startup parameter". Além disso cada transação pode cair em outra conexão do servidor.

**Por que importa:** depender de `SET` de sessão deixaria timeouts e modo de leitura sem efeito atrás de um pooler.

**Como aplicar:** `_Pool._connect` tenta com `options`, e ao ver esse erro reconecta sem elas e não tenta de novo. O caminho sem transação (`Sessions.autocommit`, usado por VACUUM e afins) depende de `SET` de sessão e por isso precisa de conexão direta ou de pooling por sessão.

**Evidência:** `tests/integration/test_pgbouncer.py` (rodar `scripts/pg-matrix.sh up bouncer`).
