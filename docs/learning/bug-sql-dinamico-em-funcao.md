---
name: SQL montado em tempo de execução dentro de função
description: função PL/pgSQL com EXECUTE lia coluna protegida, varria tabela protegida e chamava função proibida; passou a ser recusada como não verificável
type: project
category: bug
date: 2026-11-17
---
Função PL/pgSQL que contém `EXECUTE` é recusada (`function-unverifiable`) em conexão somente leitura, a não ser que esteja em `allow_functions`.

**Problema/Contexto:** a conferência lia o corpo da função e, para o texto que ela podia executar, procurava nomes proibidos dentro dos literais com uma expressão regular. Uma revisão independente derrubou isso com `'pg_read' || '_file'` (o nome nunca aparece inteiro) e com `EXECUTE 'SELECT outra_funcao()'` (a segunda função só existe dentro de um texto). O plano também não ajuda: a função é opaca nele. Resultado, em 9.6 e 18: leitura de coluna de `deny_columns`, varredura de tabela com `require_index_filter`, lock de sessão e, na 18, `pg_read_file`.

**Por que importa:** o caso real não é uma função maliciosa, é o utilitário comum `conta_linhas(tabela text)` que já existe no banco e aceita qualquer nome.

**Como aplicar:** não tentar reconhecer SQL dinâmico por padrão de texto. O que não dá para ler é recusado, como já era com PL/Python. Quem libera é o dono da configuração, pelo nome, e isso vale como aval para tudo o que a função faz.

**Evidência:** `_scan_text(..., dynamic_sql_in=...)` em `src/pgharbor/vetting.py`; funções `trap.count_rows`, `trap.glued_key`, `trap.glued_lock`, `trap.run_sql`, `trap.loops_over_sql` e `trap.wraps_dynamic` em `tests/fixture.py`; casos `function-unverifiable` em `REFUSED` e `GUARDED_REFUSED` de `tests/integration/test_guard.py`.
