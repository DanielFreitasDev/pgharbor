---
name: Cores de status como séries do gráfico
description: Usar verde, amarelo e vermelho de status para concluídas, recusadas e falhas reprovou no validador de daltonismo; as séries usam azul, amarelo, violeta e vermelho, nessa ordem.
type: project
category: failed
date: 2026-11-18
---
As quatro séries do gráfico de chamadas são, de baixo para cima: concluídas em azul, recusadas em amarelo, aprovações em violeta e falhas em vermelho. A ordem faz parte da solução.

**Problema/Contexto:** a primeira escolha foi a paleta de status (verde, amarelo de aviso, vermelho crítico, cinza para "outras"). No validador de paleta: o amarelo de aviso ficou fora da faixa de luminosidade, o cinza abaixo do piso de croma e, no tema escuro, cinza e vermelho ficaram a ΔE 3,9 para protanopia. Trocar para azul, amarelo e vermelho adjacentes também reprovou no escuro (amarelo e vermelho a ΔE 13,0 em visão normal, piso de 15).

**Por que importa:** recusas e falhas são justamente o que a tela precisa destacar; cores que um daltônico não distingue escondem o dado mais importante.

**Como aplicar:** não trocar as cores nem a ordem das séries (`--series-*` em `static/css/base.css`, `SERIES` em `static/js/charts.js`) sem rodar o validador nos dois temas, contra as superfícies `#ffffff` e `#0f1a2b`. O violeta entre amarelo e vermelho é o que separa o par problemático. Cor nunca é o único canal: há legenda, valores no foco e no ponteiro e a mesma informação em tabela.

**Evidência:** saídas do validador em 18/11/2026: claro `#2a78d6,#eda100,#6250d6,#e34948` e escuro `#3987e5,#c98500,#9085e9,#e66767`, todas as verificações aprovadas (pior par adjacente: ΔE 24,3 e 19,5 para daltonismo).
