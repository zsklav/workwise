#!/bin/sh
set -eu

# Runs only for an empty PostgreSQL volume. psql quotes password variables as SQL literals.
psql --set=ON_ERROR_STOP=1 --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" \
    --set=app_password="$DB_PASSWORD" --set=migration_password="$MIGRATION_DB_PASSWORD" <<'SQL'
CREATE ROLE workwise_migrator LOGIN PASSWORD :'migration_password';
CREATE ROLE workwise_app LOGIN PASSWORD :'app_password';
CREATE DATABASE workwise OWNER workwise_migrator;
CREATE DATABASE workwise_test OWNER workwise_app;
\connect workwise
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE workwise TO workwise_app;
GRANT USAGE ON SCHEMA public TO workwise_app;
GRANT USAGE, CREATE ON SCHEMA public TO workwise_migrator;
SQL
