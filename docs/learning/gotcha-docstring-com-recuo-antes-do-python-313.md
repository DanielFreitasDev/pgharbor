---
name: Docstring guarda o recuo antes do Python 3.13
description: no 3.11 e no 3.12 `__doc__` vem com os espaços do código; texto derivado de docstring ou de `str(tipo)` muda conforme a versão
type: project
category: gotcha
date: 2026-11-17
---
Texto gerado a partir de docstrings ou de anotações de tipo sai diferente em cada versão do Python, a não ser que seja normalizado.

**Problema/Contexto:** a descrição de cada ferramenta vem da docstring da função. O compilador só passou a remover o recuo das linhas no Python 3.13. No 3.11 e no 3.12 a descrição enviada ao agente saía com quatro espaços em cada linha, e `docs/tools.md`, gerado no 3.14, não batia com o gerado no CI. O mesmo teste também falhava no 3.13 por outro motivo: `str()` de uma anotação imprime `Optional[...]` até o 3.13 e `... | None` no 3.14.

**Como aplicar:**
- ler docstring sempre por `inspect.cleandoc` (ou `inspect.getdoc`), nunca `__doc__` cru;
- escrever tipos pela estrutura (`typing.get_origin` e `typing.get_args`), nunca por `str(anotacao)`;
- antes de enviar mudança no gerador, rodar `uv run --python 3.11 --isolated --group dev python scripts/dev/gen_docs.py --check`.

**Evidência:** primeira execução do CI em 17/11/2026: `Unit (Python 3.11)`, `3.12` e `3.13` falharam em `test_tool_reference_is_up_to_date`, `3.14` passou. `ToolSpec.description` em `src/pgharbor/app.py`, `_render` em `scripts/dev/gen_docs.py`, teste `test_tool_descriptions_carry_no_source_indentation`.
