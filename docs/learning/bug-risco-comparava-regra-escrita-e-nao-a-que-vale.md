---
name: Risco de uma mudança comparava a regra escrita, não a que vale
description: Comparar configurações chave a chave deixava salvar sem aviso mudanças que reduzem a proteção, como uma regra `*.*` posta antes de outra em `protect`.
type: project
category: bug
date: 2026-11-18
---
O classificador de risco do painel (`ui/risks.py`) pergunta qual regra está em vigor depois da mudança, e não se a regra antiga ainda está escrita.

**Problema/Contexto:** a primeira versão indexava as regras de `protect` pelo nome da tabela e comparava uma a uma. Mas o servidor aplica a **primeira** regra que casa (`ConnectionConfig.protection_for`). Colocar `{table = "*.*"}` antes da regra de `billing.cards`, ou duplicar a tabela com a primeira cópia vazia, anulava `deny_columns`, `max_rows` e `require_index_filter` com `risks = []`: o painel salvava sem pedir confirmação. Uma revisão independente achou mais casos do mesmo tipo: renomear a conexão de produção e trocar o ambiente na mesma gravação (a conexão "nova" não era comparada com a antiga), apontar uma conexão para outro servidor por `params.hostaddr` ou `service`, acrescentar `role`, mudar `import_dir`, zerar `lock_timeout` (zero é "sem limite") e mandar o log para `/dev/null` por `audit.path`.

**Por que importa:** a promessa do painel é que nenhuma mudança reduz a proteção sem um "sim" explícito. Um comparador que olha a forma do arquivo, e não o efeito, cumpre a promessa só para quem edita do jeito esperado.

**Como aplicar:**
- Para toda regra com precedência (hoje `protect`), comparar o resultado em vigor: `_first(regras, tabela)` antes e depois, para cada nome que qualquer das duas listas cita. O que não dá para decidir (padrões que se sobrepõem sem um citar o outro) vira aviso `protection-rearranged`, nunca silêncio.
- Conexão que some e reaparece com o mesmo destino é a mesma conexão renomeada: comparar com o que ela era. Conexão nova sempre pede confirmação, porque "desenvolvimento" pode apontar para qualquer lugar.
- Ao criar uma chave de configuração, perguntar: existe valor dela que deixa o agente fazer mais? Se sim, ela precisa de achado em `risks.py` e de um caso em `tests/unit/test_panel_document.py`. Valores especiais (zero como "sem limite", lista vazia como "todas") contam.

**Evidência:** `tests/unit/test_panel_document.py::test_the_rule_in_force_is_what_counts_for_a_protected_table` e `tests/unit/test_panel_api.py::test_a_weakening_hidden_in_the_shape_of_the_change_still_needs_a_yes` (dez mudanças que antes eram gravadas sem aviso); revisão de segurança por subagente em 18/11/2026.
