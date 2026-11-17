---
name: Container não lê o arquivo de configuração privado
description: a imagem roda como uid 10001 e o config.toml montado é 600 do usuário do host; é preciso --user
type: project
category: gotcha
date: 2026-11-17
---
Com `-v ~/.config/pgharbor:/config:ro`, o container falha com `Permission denied` ao ler `config.toml`, porque o arquivo é modo 600 do usuário do host e a imagem roda como `pgharbor` (uid 10001).

**Como aplicar:** rodar com `--user "$(id -u):$(id -g)"`. Afrouxar a permissão do arquivo não é opção: ele pode conter senha, e o `pgharbor check` avisa quando outros conseguem ler.

**Evidência:** teste de 17/11/2026 com a imagem `pgharbor:dev`: sem `--user`, `pgharbor: /config/config.toml: Permission denied`; com `--user`, `ok local read-only PostgreSQL 18.6`.
