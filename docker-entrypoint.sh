#!/bin/sh
# Writes the crontab from CRON_SCHEDULE, then runs the given command.
set -eu

CRON_SCHEDULE="${CRON_SCHEDULE:-*/5 * * * *}"
CRONTAB_PATH="${CRONTAB_PATH:-/tmp/crontab}"

# Fail at container start rather than at the first scheduled run.
missing=""
for name in IMAP_HOST IMAP_USERNAME IMAP_PASSWORD PAPRA_BASE_URL PAPRA_API_KEY PAPRA_ORGANIZATION_ID; do
    eval "value=\${${name}:-}"
    [ -n "${value}" ] || missing="${missing} ${name}"
done

if [ -n "${missing}" ]; then
    echo "imap-to-papra: missing required setting(s):${missing}" >&2
    echo "imap-to-papra: pass them in with env_file, e.g. cp .env.example .env" >&2
    exit 2
fi

printf '%s imap-to-papra\n' "${CRON_SCHEDULE}" > "${CRONTAB_PATH}"
echo "imap-to-papra: schedule is '${CRON_SCHEDULE}'"

exec "$@"
