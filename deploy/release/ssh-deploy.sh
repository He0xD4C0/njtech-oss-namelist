#!/bin/bash
set -euo pipefail
: "${DEPLOY_HOST:?}" "${DEPLOY_USER:?}" "${DEPLOY_SSH_KEY:?}" "${DEPLOY_KNOWN_HOSTS:?}" "${RELEASE_ID:?}" "${RELEASE_ENVIRONMENT:?}"
[[ "$DEPLOY_HOST" =~ ^[a-zA-Z0-9.-]+$ && "$DEPLOY_USER" =~ ^[a-z_][a-z0-9_-]*$ && "$RELEASE_ID" =~ ^[a-zA-Z0-9-]{1,100}$ ]] || exit 64
case "$RELEASE_ENVIRONMENT" in staging|production) ;; *) exit 64 ;; esac
scratch=$(mktemp -d)
cleanup() { rm -r -- "$scratch"; }
trap cleanup EXIT
chmod 700 "$scratch"
printf '%s\n' "$DEPLOY_SSH_KEY" > "$scratch/key"
printf '%s\n' "$DEPLOY_KNOWN_HOSTS" > "$scratch/known_hosts"
chmod 600 "$scratch/key" "$scratch/known_hosts"
options=(-i "$scratch/key" -o BatchMode=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$scratch/known_hosts" -o ConnectTimeout=15)
remote="$DEPLOY_USER@$DEPLOY_HOST"
directory="/srv/namelist/releases/$RELEASE_ID"
ssh "${options[@]}" "$remote" "mkdir -p '$directory'"
scp "${options[@]}" release/deployment.tar.gz "$remote:$directory/deployment.tar.gz"
ssh "${options[@]}" "$remote" "cd '$directory' && tar -xzf deployment.tar.gz && bash deploy/release/deploy.sh '$RELEASE_ENVIRONMENT'"
