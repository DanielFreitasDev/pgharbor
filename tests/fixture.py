"""The sample database used by the integration tests.

``statements(version_num)`` returns the SQL that builds it on a given server version.
The database is small but touches most of what PostgreSQL offers, plus a few objects
that exist only to be refused by the safety checks (see the ``trap`` schema).

Bump ``FIXTURE_VERSION`` whenever the content changes: servers are rebuilt only when the
stored version differs.
"""

from __future__ import annotations

FIXTURE_VERSION = "14"
DATABASE = "pgharbor_test"
LIMITED_ROLE = "harbor_ro"
LIMITED_PASSWORD = "harbor_ro_pw"

# Contrib extensions shipped with the official images. Missing ones are skipped.
EXTENSIONS = (
    "pg_stat_statements",
    "pgstattuple",
    "pg_buffercache",
    "pg_freespacemap",
    "pg_visibility",
    "pg_trgm",
    "pgcrypto",
    "dblink",
    "tablefunc",
    "postgres_fdw",
    "btree_gin",
    "hstore",
    "amcheck",
    "pg_prewarm",
)


def statements(v: int) -> list[str]:
    """SQL for a server whose ``server_version_num`` is ``v``."""
    out: list[str] = []
    add = out.append

    add("CREATE SCHEMA shop")
    add("CREATE SCHEMA vault")
    add("CREATE SCHEMA analytics")
    add("CREATE SCHEMA events")
    add("CREATE SCHEMA trap")
    add("COMMENT ON SCHEMA shop IS 'Online shop: customers, products and orders'")

    # ------------------------------------------------------------------ types
    add("CREATE TYPE shop.order_status AS ENUM ('new', 'paid', 'shipped', 'cancelled')")
    add("CREATE DOMAIN shop.email AS text CHECK (VALUE ~ '^[^@ ]+@[^@ ]+$')")
    add("CREATE TYPE shop.address AS (street text, city text, zip text)")
    add("CREATE SEQUENCE shop.invoice_seq START 1000 INCREMENT 5")
    add(
        "CREATE SEQUENCE shop.tiny_seq AS smallint MAXVALUE 100"
        if v >= 100000
        else "CREATE SEQUENCE shop.tiny_seq MAXVALUE 100"
    )
    add("SELECT setval('shop.tiny_seq', 95)")

    # ----------------------------------------------------------------- tables
    add("""
        CREATE TABLE shop.customers (
            id serial PRIMARY KEY,
            name text NOT NULL,
            email shop.email UNIQUE,
            address shop.address,
            created_at timestamptz NOT NULL DEFAULT now(),
            api_token text,
            notes text
        )""")
    add("COMMENT ON TABLE shop.customers IS 'People who buy from the shop'")
    add("COMMENT ON COLUMN shop.customers.api_token IS 'Secret token of the customer API'")
    add("""
        CREATE TABLE shop.products (
            id serial PRIMARY KEY,
            sku text NOT NULL UNIQUE,
            name text NOT NULL,
            price numeric(10,2) NOT NULL CHECK (price >= 0),
            tags text[] NOT NULL DEFAULT '{}',
            attrs jsonb NOT NULL DEFAULT '{}',
            photo bytea
        )""")
    add("""
        CREATE TABLE shop.orders (
            id bigserial PRIMARY KEY,
            customer_id int NOT NULL REFERENCES shop.customers (id),
            status shop.order_status NOT NULL DEFAULT 'new',
            total numeric(12,2) NOT NULL DEFAULT 0,
            placed_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz
        )""")
    add("""
        CREATE TABLE shop.order_items (
            order_id bigint NOT NULL REFERENCES shop.orders (id) ON DELETE CASCADE,
            product_id int NOT NULL REFERENCES shop.products (id),
            qty int NOT NULL CHECK (qty > 0),
            PRIMARY KEY (order_id, product_id)
        )""")
    # A foreign key without a supporting index, for the index advisor.
    add("""
        CREATE TABLE shop.reviews (
            id serial PRIMARY KEY,
            product_id int NOT NULL REFERENCES shop.products (id),
            customer_id int REFERENCES shop.customers (id),
            stars smallint NOT NULL,
            body text
        )""")
    add("CREATE TABLE shop.no_pk (code text, label text)")
    if v >= 100000:
        add("""
            CREATE TABLE shop.invoices (
                id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                order_id bigint NOT NULL REFERENCES shop.orders (id),
                amount numeric(12,2) NOT NULL
            )""")
    if v >= 120000:
        add("""
            CREATE TABLE shop.lines (
                id serial PRIMARY KEY,
                net numeric(10,2) NOT NULL,
                tax numeric(10,2) NOT NULL,
                gross numeric(10,2) GENERATED ALWAYS AS (net + tax) STORED
            )""")

    # ---------------------------------------------------------------- indexes
    add("CREATE INDEX orders_customer_idx ON shop.orders (customer_id)")
    add("CREATE INDEX orders_customer_dup_idx ON shop.orders (customer_id)")
    add("CREATE INDEX orders_placed_idx ON shop.orders (placed_at)")
    add("CREATE INDEX orders_paid_idx ON shop.orders (placed_at) WHERE status = 'paid'")
    add("CREATE INDEX customers_lower_name_idx ON shop.customers (lower(name))")
    add("CREATE INDEX products_attrs_idx ON shop.products USING gin (attrs)")
    add("CREATE INDEX products_tags_idx ON shop.products USING gin (tags)")

    # ------------------------------------------------------- views and matviews
    add("""
        CREATE VIEW shop.order_summary AS
        SELECT o.id, c.name AS customer, o.status, o.total, o.placed_at,
               (SELECT count(*) FROM shop.order_items i WHERE i.order_id = o.id) AS items
          FROM shop.orders o JOIN shop.customers c ON c.id = o.customer_id""")
    add("COMMENT ON VIEW shop.order_summary IS 'One row per order with customer name'")
    add("""
        CREATE VIEW shop.customers_public AS
        SELECT id, name, created_at FROM shop.customers""")
    add("""
        CREATE MATERIALIZED VIEW analytics.daily_sales AS
        SELECT date_trunc('day', placed_at)::date AS day, count(*) AS orders, sum(total) AS revenue
          FROM shop.orders GROUP BY 1""")
    add("CREATE UNIQUE INDEX daily_sales_day_idx ON analytics.daily_sales (day)")

    # -------------------------------------------------------------- functions
    add("""
        CREATE FUNCTION shop.fmt_price(numeric) RETURNS text
        LANGUAGE sql IMMUTABLE AS $$ SELECT 'R$ ' || to_char($1, 'FM999G999G990D00') $$""")
    add("""
        CREATE FUNCTION shop.order_total(bigint) RETURNS numeric
        LANGUAGE sql STABLE AS $$
            SELECT coalesce(sum(i.qty * p.price), 0)
              FROM shop.order_items i JOIN shop.products p ON p.id = i.product_id
             WHERE i.order_id = $1 $$""")
    add("COMMENT ON FUNCTION shop.order_total(bigint) IS 'Sum of the items of an order'")
    add("""
        CREATE FUNCTION shop.touch_order(bigint) RETURNS void
        LANGUAGE plpgsql VOLATILE AS $$
        BEGIN
            UPDATE shop.orders SET updated_at = now() WHERE id = $1;
        END $$""")
    add("""
        CREATE FUNCTION shop.set_updated_at() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            NEW.updated_at := now();
            RETURN NEW;
        END $$""")
    add("""
        CREATE TRIGGER orders_updated_at BEFORE UPDATE ON shop.orders
        FOR EACH ROW EXECUTE PROCEDURE shop.set_updated_at()""")
    add("""
        CREATE FUNCTION shop.customer_label(shop.customers) RETURNS text
        LANGUAGE sql STABLE AS $$ SELECT $1.name || ' <' || coalesce($1.email, '?') || '>' $$""")
    if v >= 110000:
        add("""
            CREATE PROCEDURE shop.archive_orders(before date)
            LANGUAGE plpgsql AS $$
            BEGIN
                DELETE FROM shop.orders WHERE placed_at < before AND status = 'cancelled';
            END $$""")

    # ------------------------------------------------ row security and privileges
    add("CREATE TABLE shop.tenant_docs (id serial PRIMARY KEY, tenant text NOT NULL, body text)")
    add("ALTER TABLE shop.tenant_docs ENABLE ROW LEVEL SECURITY")
    add("""
        CREATE POLICY tenant_isolation ON shop.tenant_docs
        USING (tenant = current_setting('app.tenant', true))""")

    # ------------------------------------------------------------ partitioning
    if v >= 100000:
        add("""
            CREATE TABLE events.log (
                id bigint NOT NULL, created_at timestamptz NOT NULL, kind text, payload jsonb
            ) PARTITION BY RANGE (created_at)""")
        for year in (2024, 2025, 2026):
            add(f"""
                CREATE TABLE events.log_{year} PARTITION OF events.log
                FOR VALUES FROM ('{year}-01-01') TO ('{year + 1}-01-01')""")
        add("CREATE INDEX log_2026_created_idx ON events.log_2026 (created_at)")
    else:
        add("""
            CREATE TABLE events.log (
                id bigint NOT NULL, created_at timestamptz NOT NULL, kind text, payload jsonb)""")
        for year in (2024, 2025, 2026):
            add(f"""
                CREATE TABLE events.log_{year} (
                    CHECK (created_at >= '{year}-01-01' AND created_at < '{year + 1}-01-01')
                ) INHERITS (events.log)""")

    # ------------------------------------------------- the protected "vault" table
    add("""
        CREATE TABLE vault.files (
            id bigserial PRIMARY KEY,
            name text NOT NULL,
            checksum text NOT NULL,
            access_key text NOT NULL,
            folder_id int NOT NULL,
            size_bytes bigint NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )""")
    add("CREATE INDEX files_name_idx ON vault.files (name)")
    add("CREATE INDEX files_checksum_idx ON vault.files (checksum)")
    add("CREATE TABLE vault.folders (id int PRIMARY KEY, path text NOT NULL)")
    add("CREATE VIEW vault.file_keys AS SELECT id, access_key AS k FROM vault.files")
    add("CREATE VIEW vault.file_keys_twice AS SELECT id, upper(k) AS kk FROM vault.file_keys")
    add("CREATE VIEW vault.file_names AS SELECT id, name, folder_id FROM vault.files")

    # ------------------------------------------------------------------- data
    add("""
        INSERT INTO shop.customers (name, email, address, api_token, notes)
        SELECT 'Customer ' || g, 'customer' || g || '@example.com',
               ROW(g || ' Main St', 'City ' || (g % 20), lpad(g::text, 5, '0'))::shop.address,
               md5('token' || g), CASE WHEN g % 50 = 0 THEN repeat('long note ', 600) END
          FROM generate_series(1, 400) g""")
    add("""
        INSERT INTO shop.products (sku, name, price, tags, attrs, photo)
        SELECT 'SKU-' || lpad(g::text, 4, '0'), 'Product ' || g, (g * 3.5)::numeric(10,2),
               ARRAY['tag' || (g % 5), 'tag' || (g % 7)],
               json_build_object('color', (ARRAY['red','green','blue'])[1 + g % 3], 'size', g % 10)::jsonb,
               CASE WHEN g % 40 = 0 THEN decode(repeat('ab', 4000), 'hex') END
          FROM generate_series(1, 120) g""")
    add("""
        INSERT INTO shop.orders (customer_id, status, total, placed_at)
        SELECT 1 + (g * 7) % 400,
               (ARRAY['new','paid','shipped','cancelled'])[1 + g % 4]::shop.order_status,
               (g % 900 + 10)::numeric(12,2),
               timestamptz '2026-01-01 00:00:00+00' + (g || ' minutes')::interval
          FROM generate_series(1, 6000) g""")
    add("""
        INSERT INTO shop.order_items (order_id, product_id, qty)
        SELECT o.id, 1 + (o.id * k) % 120, 1 + (o.id + k) % 4
          FROM shop.orders o CROSS JOIN generate_series(1, 2) k
        ON CONFLICT DO NOTHING""")
    add("""
        INSERT INTO shop.reviews (product_id, customer_id, stars, body)
        SELECT 1 + g % 120, 1 + g % 400, 1 + g % 5, 'Review number ' || g
          FROM generate_series(1, 800) g""")
    add("INSERT INTO shop.no_pk VALUES ('a', 'Alpha'), ('b', 'Beta')")
    add("""
        INSERT INTO shop.tenant_docs (tenant, body)
        SELECT (ARRAY['acme','globex'])[1 + g % 2], 'Document ' || g FROM generate_series(1, 20) g""")
    add("""
        INSERT INTO events.log_2026 (id, created_at, kind, payload)
        SELECT g, timestamptz '2026-03-01 00:00:00+00' + (g || ' seconds')::interval,
               (ARRAY['login','logout','purchase'])[1 + g % 3], json_build_object('n', g)::jsonb
          FROM generate_series(1, 3000) g""")
    add("INSERT INTO vault.folders SELECT g, '/data/' || g FROM generate_series(1, 20) g")
    add("""
        INSERT INTO vault.files (name, checksum, access_key, folder_id, size_bytes)
        SELECT 'file-' || g || '.pdf', md5(g::text), 'KEY-' || md5('k' || g), 1 + g % 20, g * 10
          FROM generate_series(1, 20000) g""")
    if v >= 100000:
        add(
            "INSERT INTO shop.invoices (order_id, amount) SELECT id, total FROM shop.orders LIMIT 50"
        )
    if v >= 120000:
        add("INSERT INTO shop.lines (net, tax) SELECT g, g * 0.1 FROM generate_series(1, 30) g")
    add("REFRESH MATERIALIZED VIEW analytics.daily_sales")

    # ---------------------------------------------------- things that must be refused
    add("""
        CREATE TABLE trap.side_effects (
            id serial PRIMARY KEY, what text NOT NULL, at timestamptz NOT NULL DEFAULT now())""")
    # A function that lies about its volatility and writes through another connection
    # is the classic way around a read-only transaction. dblink may be absent, so the
    # body reaches it through dynamic SQL.
    add("""
        CREATE FUNCTION trap.lying_stable() RETURNS int
        LANGUAGE plpgsql STABLE AS $$
        BEGIN
            EXECUTE 'SELECT pg_sleep(0)';
            RETURN 1;
        END $$""")
    add("""
        CREATE FUNCTION trap.lying_immutable() RETURNS boolean
        LANGUAGE sql IMMUTABLE AS $$ SELECT pg_try_advisory_lock(424242) $$""")
    add("CREATE VIEW trap.view_with_sleep AS SELECT 1 AS one, (pg_sleep(0) IS NULL) AS slept")
    add("CREATE VIEW trap.view_with_liar AS SELECT trap.lying_immutable() AS locked")
    add("CREATE VIEW trap.view_over_view AS SELECT * FROM trap.view_with_liar")
    add("""
        CREATE FUNCTION trap.calls_liar() RETURNS boolean
        LANGUAGE sql STABLE AS $$ SELECT trap.lying_immutable() $$""")
    add("""
        CREATE FUNCTION trap.honest_stable(int) RETURNS int
        LANGUAGE sql STABLE AS $$ SELECT $1 + (SELECT count(*)::int FROM shop.no_pk) $$""")
    add("""
        CREATE FUNCTION trap.reads_vault() RETURNS text
        LANGUAGE sql STABLE AS $$ SELECT access_key FROM vault.files WHERE id = 1 $$""")
    add("CREATE SEQUENCE trap.counter")
    add("""
        CREATE FUNCTION trap.volatile_op(int, int) RETURNS boolean
        LANGUAGE sql VOLATILE AS $$ SELECT nextval('trap.counter') > 0 $$""")
    add("CREATE OPERATOR trap.@@@ (LEFTARG = int, RIGHTARG = int, PROCEDURE = trap.volatile_op)")

    # An aggregate is only as safe as its state function, and an operator as the function
    # behind it. Both of these hide a session lock behind an innocent-looking call.
    add("""
        CREATE FUNCTION trap.agg_state(int, int) RETURNS int
        LANGUAGE sql IMMUTABLE AS $$
            SELECT coalesce($1, 0) + $2 + (pg_try_advisory_lock(424243))::int $$""")
    add("CREATE AGGREGATE trap.sneaky_sum(int) (SFUNC = trap.agg_state, STYPE = int)")
    add("""
        CREATE FUNCTION trap.lying_op(int, int) RETURNS boolean
        LANGUAGE sql IMMUTABLE AS $$ SELECT pg_try_advisory_lock(424244) $$""")
    add("CREATE OPERATOR trap.=== (LEFTARG = int, RIGHTARG = int, PROCEDURE = trap.lying_op)")
    # SQL built at run time: nothing in these bodies says what they will read or call.
    # The first is the helper every database seems to have; the others glue the text
    # from pieces, so that no pattern over the strings can recognise it.
    add("""
        CREATE FUNCTION trap.count_rows(tbl text) RETURNS bigint
        LANGUAGE plpgsql STABLE AS $$
        DECLARE n bigint;
        BEGIN
            EXECUTE 'SELECT count(*) FROM ' || tbl INTO n;
            RETURN n;
        END $$""")
    add("""
        CREATE FUNCTION trap.glued_key(i int) RETURNS text
        LANGUAGE plpgsql STABLE AS $$
        DECLARE k text;
        BEGIN
            EXECUTE 'SELECT access' || '_key FROM vault.' || 'files WHERE id = $1' INTO k USING i;
            RETURN k;
        END $$""")
    add("""
        CREATE FUNCTION trap.glued_lock() RETURNS boolean
        LANGUAGE plpgsql STABLE AS $$
        DECLARE ok boolean;
        BEGIN
            EXECUTE 'SELECT pg_try_advisory' || '_lock(424245)' INTO ok;
            RETURN ok;
        END $$""")
    add("""
        CREATE FUNCTION trap.run_sql(q text) RETURNS SETOF text
        LANGUAGE plpgsql STABLE AS $$ BEGIN RETURN QUERY EXECUTE q; END $$""")
    add("""
        CREATE FUNCTION trap.loops_over_sql(q text) RETURNS bigint
        LANGUAGE plpgsql STABLE AS $$
        DECLARE r record; n bigint := 0;
        BEGIN
            FOR r IN EXECUTE q LOOP n := n + 1; END LOOP;
            RETURN n;
        END $$""")
    add("""
        CREATE FUNCTION trap.wraps_dynamic() RETURNS bigint
        LANGUAGE sql STABLE AS $$ SELECT trap.count_rows('vault.files') $$""")

    # Code attached to a type runs without being named by the statement: the function
    # behind a cast, the CHECK of a domain (also when a value becomes an argument of
    # that type) and the function behind an operator of the type.
    add("CREATE TYPE trap.wrapped AS (v text)")
    add("""
        CREATE FUNCTION trap.to_wrapped(int) RETURNS trap.wrapped
        LANGUAGE sql STABLE AS $$
            SELECT ROW((SELECT access_key FROM vault.files WHERE id = $1))::trap.wrapped $$""")
    add("CREATE CAST (int AS trap.wrapped) WITH FUNCTION trap.to_wrapped(int)")
    add("CREATE TYPE trap.sticky AS (v int)")
    add("""
        CREATE FUNCTION trap.to_sticky(int) RETURNS trap.sticky
        LANGUAGE sql IMMUTABLE AS $$
            SELECT ROW($1 + (pg_try_advisory_lock(424246))::int)::trap.sticky $$""")
    add("CREATE CAST (int AS trap.sticky) WITH FUNCTION trap.to_sticky(int)")
    add("""
        CREATE FUNCTION trap.locking_check(int) RETURNS boolean
        LANGUAGE sql IMMUTABLE AS $$ SELECT pg_try_advisory_lock(424247) $$""")
    add("CREATE DOMAIN trap.checked AS int CHECK (trap.locking_check(VALUE))")
    add("""
        CREATE FUNCTION trap.takes_checked(d trap.checked) RETURNS int
        LANGUAGE sql STABLE AS $$ SELECT d + 0 $$""")
    add("CREATE TYPE trap.holder AS (c trap.checked)")
    add("CREATE TYPE trap.tagged AS (v int)")
    add("""
        CREATE FUNCTION trap.tagged_eq(trap.tagged, trap.tagged) RETURNS boolean
        LANGUAGE sql IMMUTABLE AS $$ SELECT pg_try_advisory_lock(424248) $$""")
    add("""
        CREATE OPERATOR trap.= (
            LEFTARG = trap.tagged, RIGHTARG = trap.tagged, PROCEDURE = trap.tagged_eq)""")
    add("CREATE TABLE trap.tagged_rows (id int PRIMARY KEY, t trap.tagged)")
    add("INSERT INTO trap.tagged_rows VALUES (1, ROW(1)), (2, ROW(2))")

    # A custom operator is a call nobody sees when it is written inside something else.
    add("CREATE VIEW trap.view_with_operator AS SELECT 1 OPERATOR(trap.===) 2 AS locked")
    add("""
        CREATE FUNCTION trap.uses_operator() RETURNS boolean
        LANGUAGE sql STABLE AS $$ SELECT 1 OPERATOR(trap.===) 2 $$""")
    add("""
        CREATE FUNCTION trap.uses_operator_plpgsql() RETURNS boolean
        LANGUAGE plpgsql STABLE AS $$ BEGIN RETURN 1 OPERATOR(trap.===) 2; END $$""")
    add("CREATE DOMAIN trap.checked_by_operator AS int CHECK (VALUE OPERATOR(trap.===) 2)")

    # Honest counterparts, which must keep working.
    add("""
        CREATE FUNCTION shop.label_of(i int) RETURNS text
        LANGUAGE plpgsql STABLE AS $$
        DECLARE found text;
        BEGIN
            SELECT name INTO found FROM shop.products WHERE id = i;
            RETURN coalesce(found, 'none');
        END $$""")
    add("CREATE TYPE shop.money_tag AS (amount numeric, currency text)")
    add("""
        CREATE FUNCTION shop.to_money_tag(numeric) RETURNS shop.money_tag
        LANGUAGE sql IMMUTABLE AS $$ SELECT ROW($1, 'USD')::shop.money_tag $$""")
    add("CREATE CAST (numeric AS shop.money_tag) WITH FUNCTION shop.to_money_tag(numeric)")
    add("""
        CREATE FUNCTION shop.is_sku(text) RETURNS boolean
        LANGUAGE sql IMMUTABLE AS $$ SELECT $1 ~ '^[A-Z]+-[0-9]+$' $$""")
    add("CREATE DOMAIN shop.sku_code AS text CHECK (shop.is_sku(VALUE))")
    add("""
        CREATE AGGREGATE shop.sum_qty(int) (SFUNC = int4pl, STYPE = int, INITCOND = '0')""")
    add("""
        CREATE FUNCTION shop.same_parity(int, int) RETURNS boolean
        LANGUAGE sql IMMUTABLE AS $$ SELECT $1 % 2 = $2 % 2 $$""")
    add("CREATE OPERATOR shop.~~~ (LEFTARG = int, RIGHTARG = int, PROCEDURE = shop.same_parity)")
    add("""
        CREATE VIEW shop.parity_view AS
        SELECT id, id::int OPERATOR(shop.~~~) 2 AS even FROM shop.orders""")

    add("ANALYZE")
    return out


# Third-party extensions, present only on the "extras" test server.
EXTRA_EXTENSIONS = (
    "hypopg",
    "vector",
    "postgis",
    "pg_cron",
    "pg_repack",
    "pg_stat_kcache",
    "pg_qualstats",
    "pg_wait_sampling",
    "pgaudit",
)


def extras_statements(available: set[str]) -> list[str]:
    """Sample objects for the third-party extensions that are installed."""
    out: list[str] = []
    if "pg_partman" in available:
        out.append("CREATE SCHEMA IF NOT EXISTS partman")
        out.append("CREATE EXTENSION IF NOT EXISTS pg_partman SCHEMA partman")
    if "vector" in available:
        out.append("""
            CREATE TABLE shop.embeddings (
                id serial PRIMARY KEY, product_id int REFERENCES shop.products (id),
                embedding vector(3) NOT NULL)""")
        out.append("""
            INSERT INTO shop.embeddings (product_id, embedding)
            SELECT g, ('[' || g || ',' || g % 7 || ',' || g % 3 || ']')::vector
              FROM generate_series(1, 100) g""")
        out.append(
            "CREATE INDEX embeddings_hnsw_idx ON shop.embeddings "
            "USING hnsw (embedding vector_l2_ops)"
        )
    if "postgis" in available:
        out.append("""
            CREATE TABLE shop.stores (
                id serial PRIMARY KEY, name text NOT NULL, geom geometry(Point, 4326))""")
        out.append("""
            INSERT INTO shop.stores (name, geom)
            SELECT 'Store ' || g, ST_SetSRID(ST_MakePoint(-38.5 + g * 0.01, -3.7 + g * 0.01), 4326)
              FROM generate_series(1, 25) g""")
        out.append("CREATE INDEX stores_geom_idx ON shop.stores USING gist (geom)")
    if "pg_cron" in available:
        out.append("SELECT cron.schedule('nightly-analyze', '0 3 * * *', 'ANALYZE shop.orders')")
    return out


def grants() -> list[str]:
    """Privileges of the unprivileged role used by the least-privilege tests."""
    return [
        f"GRANT CONNECT ON DATABASE {DATABASE} TO {LIMITED_ROLE}",
        f"GRANT USAGE ON SCHEMA shop, analytics, events TO {LIMITED_ROLE}",
        f"GRANT SELECT ON ALL TABLES IN SCHEMA shop, analytics, events TO {LIMITED_ROLE}",
        f"REVOKE SELECT ON shop.customers FROM {LIMITED_ROLE}",
        "GRANT SELECT (id, name, email, address, created_at, notes) ON shop.customers "
        f"TO {LIMITED_ROLE}",
    ]
