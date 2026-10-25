-- Runs ONCE, when the postgres data volume is first created (docker-entrypoint-initdb.d),
-- as the bootstrap superuser. Creates the two application roles.
--
--   sentinelx_migrate  owns the schema and may run DDL. Used only by the `migrate` service.
--   sentinelx_engine   may read and write DATA only. Used by the engine; cannot change schema.
--
-- Passwords are NOT in this file. psql's \getenv reads them from the container environment
-- (ENGINE_DB_PASSWORD / MIGRATE_DB_PASSWORD, which Compose takes from .env).
-- The api has no role at all: it never talks to the database (D-013).

\getenv engine_password ENGINE_DB_PASSWORD
\getenv migrate_password MIGRATE_DB_PASSWORD
\getenv dbname POSTGRES_DB

-- Refuse to create roles with missing, short or identical passwords (fail closed).
SELECT length(:'engine_password') >= 16 AS engine_ok,
       length(:'migrate_password') >= 16 AS migrate_ok,
       :'engine_password' <> :'migrate_password' AS different \gset
\if :engine_ok
\else
  \echo 'ERROR: ENGINE_DB_PASSWORD must be set and at least 16 characters'
  SELECT 1/0;
\endif
\if :migrate_ok
\else
  \echo 'ERROR: MIGRATE_DB_PASSWORD must be set and at least 16 characters'
  SELECT 1/0;
\endif
\if :different
\else
  \echo 'ERROR: ENGINE_DB_PASSWORD and MIGRATE_DB_PASSWORD must differ'
  SELECT 1/0;
\endif

CREATE ROLE sentinelx_migrate LOGIN PASSWORD :'migrate_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE sentinelx_engine LOGIN PASSWORD :'engine_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE;

-- Nobody gets in by default; the two roles may connect.
REVOKE ALL ON DATABASE :"dbname" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"dbname" TO sentinelx_migrate, sentinelx_engine;

-- The migrate role owns the application schema. The engine may use it, not change it.
ALTER SCHEMA public OWNER TO sentinelx_migrate;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO sentinelx_engine;

-- Alembic's bookkeeping table lives here. The engine role has no access to it at all.
CREATE SCHEMA migrations AUTHORIZATION sentinelx_migrate;
REVOKE ALL ON SCHEMA migrations FROM PUBLIC;

-- Tables and sequences the migrate role creates LATER (Stage 2 onwards) automatically become
-- usable by the engine: row access on tables, and USAGE/SELECT on sequences (needed to insert
-- into tables with identity/serial columns).
ALTER DEFAULT PRIVILEGES FOR ROLE sentinelx_migrate IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO sentinelx_engine;
ALTER DEFAULT PRIVILEGES FOR ROLE sentinelx_migrate IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO sentinelx_engine;
