---
name: `os.open` no Windows abre em modo texto
description: Gravar com `os.open` + `os.write` sem `os.O_BINARY` faz o Windows trocar cada quebra de linha por CR LF; num arquivo que já tem CR LF sobra um CR solto, que não é TOML válido.
type: project
category: gotcha
date: 2026-11-18
---
Todo `os.open` do projeto que grava conteúdo recebe `getattr(os, "O_BINARY", 0)` nas flags, e o painel preserva o estilo de quebra de linha do arquivo que edita.

**Problema/Contexto:** o painel grava o arquivo de configuração com `os.open(..., 0o600)` e `os.write(fd, texto.encode())`, para controlar as permissões desde a criação. No POSIX isso grava os bytes como estão. No Windows, um descritor aberto sem `O_BINARY` fica em modo texto e traduz cada `\n` para `\r\n` na escrita. O arquivo de configuração criado no Windows já usa `\r\n`; depois da tradução cada linha terminava em `\r\r\n`, e o validador recusava com `Found invalid character '\r'`. O backup, gravado do mesmo jeito, ficava corrompido igual.

**Por que importa:** no Linux e no macOS tudo passa. Só o job `windows-latest` do CI mostrou, com 20 testes do painel falhando por causa de uma linha.

**Como aplicar:**
- `os.open` para gravar: sempre com `| getattr(os, "O_BINARY", 0)`. O atributo não existe fora do Windows, daí o `getattr`.
- Editar texto de arquivo do usuário: normalizar `\r\n` para `\n` antes de trabalhar e devolver `\r\n` no fim se o original usava, para nunca misturar os dois (`apply_ops` em `ui/document.py`).
- Em teste, comparar bytes (`read_bytes`), não texto: `read_text` e `write_text` convertem quebras de linha no Windows e escondem o defeito. Calcular a impressão digital a partir do arquivo em disco, não da constante do teste.
- Os executores Windows do GitHub definem `PGUSER` e `PGPASSWORD`: teste que espera "nenhuma conexão configurada" precisa apagar as variáveis `PG*`.

**Evidência:** execução 44367489046 do CI em 18/11/2026 (`invalid TOML: Found invalid character '\r' (at line 1, column 38)`); `tests/unit/test_panel_document.py::test_a_file_with_windows_line_breaks_keeps_them_and_is_written_byte_for_byte`.
