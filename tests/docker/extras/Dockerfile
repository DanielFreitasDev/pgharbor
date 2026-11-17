# PostgreSQL 18 with third-party extensions, for the tests of the extension tools.
# Built and started by: scripts/pg-matrix.sh up extras
FROM postgres:18

# Each package is installed on its own so that one missing package does not stop the rest.
RUN set -eux; \
    apt-get update; \
    for package in hypopg pgvector postgis-3 cron partman repack pg-stat-kcache \
                   pg-qualstats pg-wait-sampling pgaudit; do \
        apt-get install -y --no-install-recommends "postgresql-18-${package}" \
            || echo "pgharbor: package postgresql-18-${package} is not available, skipped"; \
    done; \
    rm -rf /var/lib/apt/lists/*
