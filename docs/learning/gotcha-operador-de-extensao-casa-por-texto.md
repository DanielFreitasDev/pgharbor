---
name: Operador de extensão casa por texto e arrasta uma cadeia de checagens
description: em banco com PostGIS ou pgvector, todo `=` casava com operadores da extensão e disparava até cinco idas ao catálogo
type: project
category: gotcha
date: 2026-11-17
---
Operador próprio é reconhecido pelo texto (`=`, `&&`), porque o analisador não sabe os tipos dos operandos. Em banco com extensões que definem operadores com nomes comuns, isso alcança muita coisa.

**Problema/Contexto:** no servidor de teste com PostGIS e pgvector, `SELECT * FROM uma_view` dava cinco rodadas de sonda e estourava o limite (`verification-depth`): `=` casava com `geometry_eq` e `vector_eq`; as assinaturas levavam aos tipos `geometry` e `geography`; os operadores desses tipos escritos em SQL tinham corpo com `&&` e `@`, que casavam com mais operadores.

**Como aplicar:**
- só casa por texto o operador cujos operandos são tipos embutidos, ou o escrito em linguagem procedural sobre um tipo que um cast implícito consegue produzir;
- função de operador em C é julgada na hora, pela linha do catálogo, sem nova sonda;
- só a assinatura de função **chamada pelo nome** leva a tipos novos; a de função alcançada como código de operador ou de cast, não;
- o limite de rodadas é 8.

**Evidência:** rastreio de 17/11/2026 no servidor `extras` (porta 55500): 5 rodadas antes, 2 a 3 depois; `tests/integration/test_health.py` (conselheiro de índices) voltou a passar nesse servidor.
