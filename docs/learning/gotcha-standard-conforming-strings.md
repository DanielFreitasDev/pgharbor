---
name: standard_conforming_strings desligado muda onde o texto termina
description: com standard_conforming_strings = off a barra invertida escapa a aspa em texto comum, e o servidor passa a ver código onde o analisador vê texto
type: project
category: gotcha
date: 2026-11-17
---
Toda transação força `standard_conforming_strings = on` e confere o valor antes de executar qualquer coisa.

**Problema/Contexto:** `SELECT 'a\'' , 1 -- '` é uma coluna com o parâmetro ligado e duas com ele desligado: no segundo caso `\'` vira aspa escapada, o texto termina antes e o resto é código. Foi assim que um validador baseado em tokens foi contornado em outra ferramenta.

**Por que importa:** o analisador léxico assume o padrão (`on`). Um banco ou papel configurado com `off` abriria a diferença.

**Como aplicar:** `session._begin_sql` inclui o `SET LOCAL` e `_begin` recusa a transação se a leitura de volta não for `on`.

**Evidência:** `session._begin`; comportamento conferido em 9.6 e 18 durante a construção.
