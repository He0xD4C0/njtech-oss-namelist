#!/bin/sh
set -eu

# Read fixed server-secret names only. Never echo credentials or enable shell tracing.
for key in DJANGO_SECRET_KEY APP_ENCRYPTION_KEY BOOTSTRAP_SECRET POSTGRES_PASSWORD; do
    file_key="${key}_FILE"
    file=$(printenv "$file_key" || true)
    if [ -n "$file" ]; then
        if [ -n "$(printenv "$key" || true)" ]; then
            echo "Both $key and $file_key were supplied; refusing ambiguous credentials" >&2
            exit 1
        fi
        [ -r "$file" ] || { echo "Cannot read $file_key" >&2; exit 1; }
        value=$(cat "$file")
        [ -n "$value" ] || { echo "Empty $file_key" >&2; exit 1; }
        export "$key=$value"
    fi
done

if [ "${1:-}" = initialize ]; then
    shift
    python manage.py check --deploy --fail-level ERROR
    python manage.py migrate --noinput
    # Static files are collected into the immutable image at build time.
    python manage.py setup_periodic_tasks
    exit 0
fi

exec "$@"
