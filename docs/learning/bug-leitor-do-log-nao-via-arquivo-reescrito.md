---
name: Leitor incremental do log não percebia arquivo reescrito
description: Ler só os bytes novos do log de auditoria mantinha entradas antigas quando o arquivo era reescrito no lugar com tamanho igual ou maior.
type: project
category: bug
date: 2026-11-18
---
O leitor do painel guarda os primeiros 256 bytes do log e recomeça a leitura quando eles mudam, além de recomeçar quando o arquivo encolhe ou troca de inode.

**Problema/Contexto:** para não reler um log grande a cada 4 segundos, `AuditReader` lê a partir do último deslocamento. Reescrever o arquivo no lugar (mesmo inode) com tamanho igual ou maior não era detectado: a tela continuava mostrando as entradas antigas.

**Por que importa:** o sintoma engana: a tela mostra dados plausíveis, só que do arquivo anterior. Apareceu como entradas "daqui a 3 horas" depois de regerar um log de demonstração.

**Como aplicar:** leitura incremental de arquivo precisa de três sinais para recomeçar: inode diferente, tamanho menor que o deslocamento e início do arquivo diferente. O log real só cresce, então o custo é uma leitura de 256 bytes por atualização.

**Evidência:** `src/pgharbor/ui/activity.py::AuditReader.refresh`; `tests/unit/test_panel_api.py::test_a_log_that_is_cut_or_replaced_is_read_again` (caso "rewritten in place").
