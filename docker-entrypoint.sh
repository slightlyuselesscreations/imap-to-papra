#!/bin/sh
# Writes the crontab from CRON_SCHEDULE, then runs the given command.
set -eu

CRON_SCHEDULE="${CRON_SCHEDULE:-*/5 * * * *}"
CRONTAB_PATH="${CRONTAB_PATH:-/tmp/crontab}"

if [ ! -f "${IMAP_TO_PAPRA_CONFIG:-/config/config.toml}" ]; then
    echo "imap-to-papra: no config at ${IMAP_TO_PAPRA_CONFIG:-/config/config.toml}" >&2
    echo "imap-to-papra: mount one, e.g. -v ./config.toml:/config/config.toml:ro" >&2
    exit 2
fi

printf '%s imap-to-papra\n' "${CRON_SCHEDULE}" > "${CRONTAB_PATH}"
echo "imap-to-papra: schedule is '${CRON_SCHEDULE}'"

exec "$@"
