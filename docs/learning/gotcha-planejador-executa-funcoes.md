---
name: O planejador executa funções
description: EXPLAIN e qualquer planejamento (inclusive LIMIT 0) já executam funções IMMUTABLE com argumentos constantes e STABLE usadas em condições
type: project
category: gotcha
date: 2026-11-17
---
A conferência de funções pelo catálogo (`vetting.vet_statement`) roda antes de qualquer coisa que faça o servidor planejar a instrução.

**Problema/Contexto:** o planejador avalia funções IMMUTABLE com argumentos constantes e funções STABLE em condições indexáveis. Uma função declarada IMMUTABLE que chama `dblink_exec` escreve já no `EXPLAIN`.

**Por que importa:** o "pré-voo" pelo plano e a descrição do resultado (`LIMIT 0`) não são gratuitos: são execução parcial.

**Como aplicar:** ordem fixa em `runner.run_query`: analisar, conferir no catálogo, só então descrever, planejar e executar. Ferramenta nova que rode `EXPLAIN` sobre SQL de fora usa `runner.estimate_plan`.

**Evidência:** ordem das chamadas em `runner.run_query`; casos `trap.lying_immutable()` e `trap.view_with_liar` em `REFUSED`.
