---
name: Endereço impresso antes de a porta aceitar conexões
description: Fazer `bind` sem `listen` e imprimir o endereço deixa uma janela em que quem abre o endereço recebe conexão recusada.
type: project
category: gotcha
date: 2026-11-18
---
O painel faz `bind` e `listen` no próprio socket antes de imprimir o endereço e só depois entrega o socket ao uvicorn.

**Problema/Contexto:** o socket é criado pelo pgharbor para saber a porta real (com `--port 0`) e dar uma mensagem clara se a porta estiver ocupada. Com apenas `bind`, o uvicorn só chamava `listen` ao iniciar, depois de o endereço já ter sido impresso e de o navegador ter sido aberto.

**Por que importa:** o sintoma é intermitente: "conexão recusada" ao abrir o painel rápido demais, exatamente o que o `webbrowser.open` automático faz.

**Como aplicar:** sempre que o processo anunciar um endereço antes de o servidor HTTP subir, chamar `listen()` antes do anúncio; as conexões ficam na fila até o servidor aceitar.

**Evidência:** `tests/unit/test_panel_gate.py::test_the_real_command_serves_the_panel_and_stops` falhava com `ConnectionRefusedError: [Errno 111]` em 18/11/2026 e passou depois do `sock.listen(64)` em `src/pgharbor/ui/server.py::_listen`.
