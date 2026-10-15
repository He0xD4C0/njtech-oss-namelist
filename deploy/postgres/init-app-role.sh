#!/bin/sh
set -eu
[ "${POSTGRES_USER:-}" = postgres ] || { echo 'Use a separate postgres administrator role' >&2; exit 1; }
if [ -n "${APP_DB_PASSWORD_FILE:-}" ]; then
    app_password=$(cat "$APP_DB_PASSWORD_FILE")
else
    app_password=${APP_DB_PASSWORD:?Application database password is required}
fi
[ "$APP_DB_USER" != postgres ] || { echo 'Application role cannot be postgres' >&2; exit 1; }
psql --username postgres --dbname "$POSTGRES_DB" --set=ON_ERROR_STOP=1 \
    --set=app_user="$APP_DB_USER" --set=app_password="$app_password" --set=app_db="$POSTGRES_DB" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %L', :'app_user', :'app_password') \gexec
SELECT format('ALTER DATABASE %I OWNER TO %I', :'app_db', :'app_user') \gexec
SQL
