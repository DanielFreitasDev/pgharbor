---
name: Resposta longa chegava como JSON cortado no meio
description: o orçamento de linhas (60 mil caracteres) era maior que o teto geral de saída (48 mil); o corte final era por texto
type: project
category: bug
date: 2026-11-17
---
Dois limites de tamanho precisam ser coerentes: o orçamento de linhas de uma consulta (`max_result_chars`) e o teto de qualquer resposta (`MAX_OUTPUT_CHARS`).

**Problema/Contexto:** `max_result_chars` valia 60.000 por padrão e o invólucro das ferramentas cortava em 48.000, por texto. `SELECT g, repeat('x', 1900) FROM generate_series(1, 200) g` devolvia 48.102 caracteres de JSON inválido. Nenhum teste pegava, porque os testes de limite usavam valores pequenos.

**Como aplicar:** o padrão de `max_result_chars` é 40.000. O teto geral, quando alcançado por uma resposta tabular, tira linhas inteiras do fim, marca `has_more` e inclui `output_cut`; só o que não é tabela é cortado por texto. Ao criar outro limite de tamanho, testar com um resultado maior que todos os limites juntos.

**Evidência:** `_fit` em `src/pgharbor/app.py`; teste `test_answers_that_are_too_long_stay_valid_and_say_so` em `tests/integration/test_core.py`.
