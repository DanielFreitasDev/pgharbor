---
name: `os.kill(pid, 0)` mata o processo no Windows
description: Sondar se um processo existe com `os.kill(pid, 0)` encerra o processo no Windows; a presença das sessões usa batimento (heartbeat) em arquivo.
type: project
category: gotcha
date: 2026-11-18
---
Para saber se um servidor ainda está vivo, o painel olha a data do arquivo da sessão, que o processo regrava a cada 15 segundos. Não sonda o PID.

**Problema/Contexto:** no POSIX, `os.kill(pid, 0)` só testa a existência do processo. No Windows, qualquer sinal diferente de `CTRL_C_EVENT` e `CTRL_BREAK_EVENT` chama `TerminateProcess`: a sonda mataria o servidor MCP da sessão. Além disso, um PID pode ser reaproveitado por outro programa.

**Por que importa:** a suíte roda no Windows (job `platforms` do CI) e o pgharbor é usado nele; uma sonda de PID derrubaria sessões reais.

**Como aplicar:** em qualquer código que precise saber se outro processo do pgharbor está vivo, usar o batimento de `presence.py` (`STALE_AFTER_SECONDS`), nunca `os.kill`. Sessão sem batimento há mais de 50 segundos aparece como "encerrada sem aviso".

**Evidência:** documentação de `os.kill` do Python ("Any other value for sig will cause the process to be unconditionally killed by the TerminateProcess API"); `src/pgharbor/presence.py`; `tests/unit/test_presence_pause.py::test_presence_lifecycle`.
