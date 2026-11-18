---
name: Painel de gerenciamento é outro programa
description: O painel (`pgharbor ui`) roda separado do servidor MCP, com chave no cabeçalho Authorization e sem cookie; mudanças valem para sessões novas e só a pausa é imediata.
type: project
category: decision
date: 2026-11-18
---
O painel web é um processo à parte, iniciado por uma pessoa; o servidor MCP nunca o carrega nem abre porta. A chave de cada execução vai no fragmento da URL, fica no `sessionStorage` da aba e viaja no cabeçalho `Authorization`. Não há cookie.

**Problema/Contexto:** o transporte é stdio, então não existe servidor central: cada sessão de cliente sobe o seu processo. Uma tela que edita as regras é o alvo mais valioso do produto. A primeira ideia era trocar a chave por um cookie `HttpOnly` + `SameSite=Strict`.

**Por que importa:** cookies de `127.0.0.1` valem para todas as portas. Qualquer outro servidor local receberia a chave em cada requisição do navegador e poderia sobrescrevê-la; o prefixo `__Host-` exige `Secure`, que Chrome e Safari recusam em `http://localhost`. Com cabeçalho, uma página estrangeira não consegue fazer o navegador enviar a chave, então não há requisição forjada que funcione. É o mesmo modelo que o MCP Inspector adotou depois da CVE-2025-49596.

**Como aplicar:**
- Nada em `src/pgharbor/ui/` pode ser importado pelo caminho do servidor (`cli.py` importa só dentro do comando `ui`).
- Não introduzir cookie nem aceitar a chave na query string.
- `Host`, `Origin` e `Sec-Fetch-Site` são comparados por igualdade, nunca por expressão regular (foi assim que o Jupyter Server foi contornado em 2026, GHSA-24qx-w28j-9m6p).
- Alteração de configuração vale no início da próxima sessão; a tela mostra quais sessões estão com a versão antiga. Recarregar regras no meio de uma sessão foi descartado por ser mais difícil de auditar.
- O único canal do painel para sessões em execução é a pausa (`pause.py`), que só tira acesso e falha fechada.

**Alternativas descartadas:** painel dentro do processo MCP (abriria porta em todo servidor); cookie de sessão; recarga a quente da configuração.

**Evidência:** `src/pgharbor/ui/gate.py`; `tests/unit/test_panel_gate.py` (53 casos de requisição que não podem funcionar, incluindo chave em cookie e na URL); `docs/security.md`, seção "The management panel"; pesquisa de 18/11/2026 sobre Jupyter, MCP Inspector, Vite e a especificação do MCP.
