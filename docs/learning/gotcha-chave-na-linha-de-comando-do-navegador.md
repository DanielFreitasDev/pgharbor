---
name: Abrir o navegador com a chave na URL expõe a chave na lista de processos
description: `webbrowser.open(url)` passa a URL como argumento de linha de comando, legível por qualquer usuário da máquina; o painel entrega a chave por um arquivo privado.
type: project
category: gotcha
date: 2026-11-18
---
O painel abre o navegador num arquivo HTML privado (modo 600, no diretório de estado) que redireciona para o endereço com a chave, e apaga o arquivo em seguida. O navegador nunca recebe a chave como argumento.

**Problema/Contexto:** `webbrowser.open("http://127.0.0.1:7432/#token=...")` executa `xdg-open` (ou o navegador) com o endereço no argv. Sem `hidepid` no `/proc`, que é o padrão, `ps -eo args` mostra o argv de todos os processos a qualquer usuário. Se o navegador ainda não estava aberto, o processo dele carrega a chave enquanto viver.

**Por que importa:** o modelo de ameaças do painel diz que outro usuário da máquina alcança a porta, mas não tem a chave. Com a chave no argv, tinha.

**Como aplicar:** segredo nunca vai em argumento de linha de comando, nem "só para abrir o navegador". É a mesma solução do Jupyter (`jpserver-<pid>-open.html`). Navegador em sandbox que não lê o diretório de estado (alguns pacotes snap) mostra erro; por isso o terminal continua imprimindo o endereço.

**Evidência:** reproduzido em 18/11/2026 com `BROWSER="sh -c 'sleep 3; true' %s"` e `ps -eo user,pid,args`; `src/pgharbor/ui/server.py::_handoff_page`; `tests/unit/test_panel_api.py::test_the_browser_is_never_given_the_key_on_a_command_line`.
