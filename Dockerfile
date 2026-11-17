# pgharbor in a container. The server speaks MCP over stdio, so run it with -i:
#
#   docker build -t pgharbor .
#   docker run -i --rm -e PGHARBOR_DSN="postgresql://user:password@host:5432/db" pgharbor
#
# To use a configuration file, mount it and point PGHARBOR_CONFIG at it. The file is
# private to you (mode 600), so run the container as yourself to let it be read:
#
#   docker run -i --rm --user "$(id -u):$(id -g)" \
#     -v "$HOME/.config/pgharbor:/config:ro" \
#     -e PGHARBOR_CONFIG=/config/config.toml -e APP_DB_PASSWORD pgharbor
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --system --create-home --uid 10001 pgharbor

USER pgharbor
ENV XDG_STATE_HOME=/home/pgharbor/.local/state
ENTRYPOINT ["pgharbor"]
