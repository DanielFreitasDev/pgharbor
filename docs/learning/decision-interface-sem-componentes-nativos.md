---
name: Interface sem componentes nativos do navegador
description: Pedido explícito do responsável: nada na tela do painel pode ter aparência nativa do navegador (lista suspensa, caixa de marcação, dica, barra de rolagem).
type: feedback
category: decision
date: 2026-11-18
---
Todo controle da página do painel é um componente próprio, estilizado pelo projeto. Isso vale para os que parecem inofensivos: lista suspensa, caixa de marcação, dica flutuante, barra de rolagem, campo numérico e alça de redimensionar.

**Problema/Contexto:** a primeira versão usava `<select>`, `<input type="checkbox">` com `accent-color`, `<input type="number">`, `<datalist>`, o atributo `title` e a barra de rolagem padrão. O responsável pelo projeto pediu, em 18/11/2026, que nenhum componente fosse nativo.

**Por que importa:** é uma decisão de produto, não um gosto do momento; reintroduzir um controle nativo é regressão.

**Como aplicar:**
- Lista suspensa: `dropdown()` em `static/js/ui.js` (padrão combobox, teclado completo, a lista fica dentro do `<dialog>` quando aberta num diálogo).
- Caixa de marcação: `checkbox()`; o `<input>` continua existindo (teclado, leitor de tela), só o desenho é próprio.
- Dica: atributo `data-tip` e `initTooltips()`; nunca `title`.
- Números: `type="text"` com `inputmode`; nunca `type="number"`.
- Barra de rolagem: regras em `static/css/base.css`; em tela de toque só a cor muda, porque lá a barra flutua e não ocupa espaço.
- O `<dialog>` nativo é mantido porque dá de graça o foco preso, o Escape e a devolução do foco; a aparência é toda do projeto.
- O aviso do navegador ao fechar a aba (`beforeunload`) não é usado, porque não pode ser estilizado: alterações não salvas são protegidas só na navegação interna.

**Evidência:** instrução explícita do responsável em 18/11/2026; `tests/unit/test_panel_document.py::test_the_page_is_made_only_of_its_own_files`.
