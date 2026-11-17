# LEARNING.md — base de conhecimento do projeto

Leia as entradas relacionadas à tarefa. Os detalhes ficam em `docs/learning/`.

## Bugs e soluções

- [Plano de execução traz nome de função dentro de texto](docs/learning/bug-nome-de-funcao-em-texto-do-plano.md) — procurar chamadas proibidas no plano com expressão regular recusava consultas legítimas; o plano precisa passar pelo mesmo analisador léxico.
- [Negar `pg_authid` pelo plano quebra `pg_roles`](docs/learning/bug-catalogos-secretos-no-plano.md) — as views do próprio PostgreSQL leem `pg_authid`; catálogo secreto é negado pelo nome citado, não pela tabela do plano.

- [SQL montado em tempo de execução dentro de função](docs/learning/bug-sql-dinamico-em-funcao.md) — `EXECUTE` com texto colado escapava da conferência e do plano; função assim é recusada como não verificável.
- [Código que roda sem ser citado: tipo, conversão, domínio e operador](docs/learning/bug-codigo-pendurado-em-tipo-e-operador.md) — cast com função, `CHECK` de domínio e operador escrito dentro de view executavam código que ninguém conferia.
- [Resposta longa chegava como JSON cortado no meio](docs/learning/bug-resposta-cortada-no-meio-do-json.md) — o orçamento de linhas era maior que o teto geral de saída; o teto agora tira linhas inteiras.

## Pegadinhas de ambiente, dependências e bibliotecas

- [psycopg cai no protocolo simples sem parâmetros](docs/learning/gotcha-psycopg-protocolo-simples.md) — `execute(sql)` e `execute(sql, [])` aceitam várias instruções; o projeto sempre liga um parâmetro extra.
- [Transação somente leitura não é fronteira](docs/learning/gotcha-read-only-nao-e-fronteira.md) — funções, `SET`, `COPY ... PROGRAM` e locks passam por dentro de `BEGIN READ ONLY`.
- [O planejador executa funções](docs/learning/gotcha-planejador-executa-funcoes.md) — `EXPLAIN` e `LIMIT 0` já rodam funções IMMUTABLE e STABLE; a conferência de funções vem antes.
- [`::text` não é igual à saída do tipo](docs/learning/gotcha-cast-text-difere-da-saida.md) — `boolean` vira `true`, `inet` ganha `/32`, `char(n)` perde espaços.
- [`pg_depend` não registra funções de sistema](docs/learning/gotcha-pg-depend-ignora-objetos-de-sistema.md) — para saber o que uma view chama, é preciso ler `pg_get_viewdef`.
- [`ctx.elicit` falha no protocolo MCP novo](docs/learning/gotcha-elicitation-no-protocolo-novo.md) — a partir da revisão 2026-07-28 o diálogo só funciona por `Resolve` + `Elicit`.
- [PgBouncer recusa `options` na conexão](docs/learning/gotcha-pgbouncer-recusa-options.md) — os ajustes da sessão vão por `SET LOCAL` em cada transação.
- [`standard_conforming_strings = off` muda onde o texto termina](docs/learning/gotcha-standard-conforming-strings.md) — o servidor lê `'\''` diferente do analisador; a sessão força `on` e confere.

- [Operador de extensão casa por texto](docs/learning/gotcha-operador-de-extensao-casa-por-texto.md) — com PostGIS ou pgvector, todo `=` arrastava uma cadeia de checagens; só operador sobre tipo embutido casa por texto.
- [Container não lê o arquivo de configuração privado](docs/learning/gotcha-docker-arquivo-de-configuracao-600.md) — o `config.toml` montado é 600 do usuário do host; rodar com `--user`.
- [Docstring guarda o recuo antes do Python 3.13](docs/learning/gotcha-docstring-com-recuo-antes-do-python-313.md) — descrição de ferramenta e referência gerada mudavam conforme a versão; usar `inspect.cleandoc` e montar tipos pela estrutura.

## Decisões de arquitetura

- [Analisador léxico próprio, sem parser de terceiros](docs/learning/decision-lexer-proprio.md) — o `pglast` é GPL e um parser no cliente é outro programa; quem decide o tipo da instrução é o servidor.
- [Corte de linhas e de valores feito no servidor](docs/learning/decision-corte-no-servidor.md) — a consulta roda embrulhada com `left(coluna::text, N)` e `LIMIT`, para nada grande sair do banco.

## Abordagens que NÃO funcionaram

- [Descobrir colunas usadas pela saída dos nós do plano](docs/learning/failed-colunas-pela-saida-do-plano.md) — o planejador lista todas as colunas da tabela em junções; não serve para saber se uma coluna protegida foi usada.

## Notas de performance

- [Sonda do catálogo: LATERAL fora de ordem e JIT](docs/learning/perf-sonda-de-tipos-e-jit.md) — a conferência foi de 10 ms a 600 ms; medir por seção e olhar `loops=`; JIT desligado nas consultas internas.

## Referências externas

- [Catálogos reais de 9.6 a 19](docs/learning/reference-catalogos-por-versao.md) — `scripts/dev/catalog.py` consulta um retrato dos catálogos de cada versão.
