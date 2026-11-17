---
name: ctx.elicit falha na revisão 2026-07-28 do MCP
description: no protocolo novo o servidor não pode pedir nada ao cliente no meio da chamada; o diálogo só funciona pelo mecanismo Resolve + Elicit do SDK
type: project
category: gotcha
date: 2026-11-17
---
Confirmações e aprovações usam `Annotated[ElicitationResult[T], Resolve(função)]`, nunca `ctx.elicit`.

**Problema/Contexto:** com o SDK `mcp` 2.3 e a revisão 2026-07-28, `ctx.elicit()` lança `NoBackChannelError`. O `Resolve` funciona nas duas gerações: na antiga manda o pedido no meio da chamada; na nova devolve "input required" e o cliente chama de novo com a resposta.

**Por que importa:** não dá para saber de antemão qual revisão o cliente vai negociar.

**Como aplicar:** o resolvedor recebe os argumentos da ferramenta pelo nome e o `Context`; devolve `Elicit(...)` ou um valor comum. Para cliente sem suporte a diálogo ele devolve `NoDialog()`, porque pedir o diálogo assim mesmo derruba a chamada. O resolvedor pode rodar mais de uma vez: não pode ter efeito colateral.

**Evidência:** `approval.py`; `tests/integration/test_admin.py::test_writes_are_confirmed_by_the_user` nos modos `legacy` e `auto`.
