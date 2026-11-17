---
name: Catálogo secreto negado pelo plano quebra views de sistema
description: pg_roles, pg_user e pg_stat_activity leem pg_authid; negar pg_authid pelas tabelas do plano recusa essas views
type: project
category: bug
date: 2026-11-17
---
Os catálogos com credenciais (`pg_authid`, `pg_shadow`, `pg_user_mapping` e outros) são negados pelo nome que a instrução ou uma view de usuário cita, não pelas tabelas que aparecem no plano.

**Problema/Contexto:** o plano mostra tabelas-base. `SELECT rolname FROM pg_roles` aparece como varredura de `pg_authid`, porque `pg_roles` é uma view do próprio PostgreSQL que esconde a senha. Na 9.6, `pg_stat_activity` também junta com `pg_authid`. Com a negação aplicada ao plano, as três consultas eram recusadas.

**Por que importa:** essas views são o jeito correto e seguro de ler papéis e sessões.

**Como aplicar:** `vetting.relation_denied(..., configured_only=True)` no plano (vale só o `deny_relations` do perfil); a lista embutida vale na conferência por nome, que também lê a definição de views de usuário e por isso pega uma view própria feita sobre `pg_authid`.

**Evidência:** casos `SELECT rolname, rolsuper FROM pg_roles ...` e `SELECT pid, state FROM pg_stat_activity ...` em `ALLOWED`, e `SELECT * FROM pg_catalog.pg_authid` em `REFUSED`, de `tests/integration/test_guard.py`.
