# Connecting an MCP client

pgharbor is a command that speaks MCP over standard input and output. Every client is
configured the same way: tell it which command to start.

The command, wherever it is used:

```bash
uvx --from git+https://github.com/DanielFreitasDev/pgharbor pgharbor
```

`uvx` (part of [uv](https://docs.astral.sh/uv/)) downloads pgharbor into an isolated
environment the first time and reuses it afterwards. To pin a version, add the tag to the
URL: `git+https://github.com/DanielFreitasDev/pgharbor@v0.1.0`.

Prefer a permanent install? `uv tool install git+https://github.com/DanielFreitasDev/pgharbor`
(or `pipx install git+https://github.com/DanielFreitasDev/pgharbor`) puts a `pgharbor`
command on your `PATH`; then use `"command": "pgharbor"` with no arguments below.

## Where the database settings come from

- **A configuration file** (`~/.config/pgharbor/config.toml`, see
  [configuration.md](configuration.md)). Recommended. The client configuration then needs
  nothing else; pass `--config /path/file.toml` or `PGHARBOR_CONFIG` to use another file.
- **Environment variables** for a single database: `PGHARBOR_DSN` (or `DATABASE_URL`).

Credentials named in the configuration file (`user_env`, `password_env`) are read from
the environment of the pgharbor process. Clients started from a terminal inherit your
shell's variables; desktop applications usually do not, so set them in the client's
`env` block or use `password_file` / `~/.pgpass`.

## Claude Code

```bash
claude mcp add pgharbor --scope user -- uvx --from git+https://github.com/DanielFreitasDev/pgharbor pgharbor
```

- `--scope user` makes it available in every project; `--scope project` writes a
  `.mcp.json` you can commit.
- Pass variables with `--env NAME=value`, placed before another option so that the
  server name is not read as a second variable:
  `claude mcp add --env PGHARBOR_CONFIG=/path/config.toml --scope user pgharbor -- uvx ...`.
- `claude mcp list` shows whether it connected; `/mcp` inside a session shows its tools.
- To skip the permission prompt for the read tools, allow them in `settings.json`:

  ```json
  { "permissions": { "allow": ["mcp__pgharbor__pg_query", "mcp__pgharbor__pg_list_tables",
                               "mcp__pgharbor__pg_describe_table"] } }
  ```

  Leave `pg_execute` and the other writing tools out: they ask on every call by design.
- Claude Code loads tool definitions on demand. pgharbor's server instructions tell it
  when to look, and `pg_connections`, `pg_query` and `pg_request_access` are always loaded.

A project-level `.mcp.json`:

```json
{
  "mcpServers": {
    "pgharbor": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/DanielFreitasDev/pgharbor", "pgharbor"],
      "env": { "PGHARBOR_CONFIG": "${HOME}/.config/pgharbor/config.toml" }
    }
  }
}
```

## Claude Desktop, Cursor, Windsurf and other `mcpServers` clients

Add to the client's MCP configuration file (`claude_desktop_config.json`,
`~/.cursor/mcp.json`, `~/.codeium/windsurf/mcp_config.json`):

```json
{
  "mcpServers": {
    "pgharbor": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/DanielFreitasDev/pgharbor", "pgharbor"],
      "env": { "PGHARBOR_DSN": "postgresql://user:password@localhost:5432/mydb" }
    }
  }
}
```

Desktop applications may not see your shell's `PATH`: if the client reports that `uvx`
was not found, use its full path (`which uvx`).

## Codex CLI

```toml
# ~/.codex/config.toml
[mcp_servers.pgharbor]
command = "uvx"
args = ["--from", "git+https://github.com/DanielFreitasDev/pgharbor", "pgharbor"]
env_vars = ["PGHARBOR_DSN"]        # forward variables from your shell
startup_timeout_sec = 30
```

## VS Code (GitHub Copilot)

```json
// .mcp.json at the workspace root
{
  "mcpServers": {
    "pgharbor": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "git+https://github.com/DanielFreitasDev/pgharbor", "pgharbor"]
    }
  }
}
```

For yourself in every workspace, put the same block in `~/.copilot/mcp-config.json`.
Older VS Code versions read `.vscode/mcp.json`, where the top-level key is `"servers"`
instead of `"mcpServers"`; VS Code has announced the removal of that format for
February 2027.

## Gemini CLI

```json
// ~/.gemini/settings.json
{
  "mcpServers": {
    "pgharbor": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/DanielFreitasDev/pgharbor", "pgharbor"]
    }
  }
}
```

## Dialogs

Approving a production connection and confirming a write use MCP *elicitation*: the
client shows a dialog and you answer. Claude Code supports it. A client that does not
will get a clear refusal for those two actions unless the connection profile allows the
"conversation" fallback (see [configuration.md](configuration.md#what-is-allowed)).

In a non-interactive run (`claude -p`) nobody is there to answer, and Claude Code
declines the dialog by itself: approvals and confirmations are refused, which is the safe
outcome. A script that really needs such a connection should get a profile of its own
with `approval = "never"`.

## Docker

The repository has a `Dockerfile`, for when you would rather not install Python or uv:

```bash
docker build -t pgharbor https://github.com/DanielFreitasDev/pgharbor.git
```

The server speaks MCP over stdio, so the client starts the container with `-i`:

```json
{
  "mcpServers": {
    "pgharbor": {
      "command": "docker",
      "args": ["run", "-i", "--rm", "--user", "1000:1000",
               "-v", "/home/you/.config/pgharbor:/config:ro",
               "-e", "PGHARBOR_CONFIG=/config/config.toml",
               "-e", "APP_DB_PASSWORD",
               "pgharbor"]
    }
  }
}
```

- `--user` is your own user and group id (`id -u`, `id -g`): the configuration file is
  private to you, and the container's default user could not read it.
- `-e NAME` without a value passes the variable from the client's environment into the
  container; that is how the password named by `password_env` gets in.
- A database on your own machine is not `localhost` inside the container. Use
  `host.docker.internal` as the host (on Linux, add
  `"--add-host=host.docker.internal:host-gateway"`), or `"--network", "host"`.
- The audit log stays inside the container and is lost with it. To keep it, mount a
  directory and set `[audit] path` to a file inside it.

## Checking that it works

```bash
uvx --from git+https://github.com/DanielFreitasDev/pgharbor pgharbor check
```

prints one line per connection: reachable or not, PostgreSQL version, database, role.
Connections that need approval (production) are skipped unless you name them
(`pgharbor check prod`) or pass `--all`. `pgharbor tools` lists the tools your
configuration exposes.

If the client says the server failed to start, run the command yourself in a terminal:
configuration errors are printed there. pgharbor writes logs to standard error only;
standard output belongs to the protocol.
