FROM python:3.12-slim

# supercronic runs the schedule inside the container and logs to stdout.
# media-types provides /etc/mime.types. Without it Python cannot resolve .docx or
# .xlsx and those attachments end up in Papra as unsearchable binaries.
ARG SUPERCRONIC_VERSION=v0.2.48

RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "${arch}" in \
      amd64) sha1=016b7c9aebfc8d9fd9526e8ba33b191fc524485f ;; \
      arm64) sha1=2ab9b3bdcf290f60b59700aad876b6e68f3a6b06 ;; \
      *) echo "unsupported architecture: ${arch}" >&2; exit 1 ;; \
    esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates media-types; \
    rm -rf /var/lib/apt/lists/*; \
    curl -fsSLO "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${arch}"; \
    echo "${sha1}  supercronic-linux-${arch}" | sha1sum -c -; \
    chmod +x "supercronic-linux-${arch}"; \
    mv "supercronic-linux-${arch}" /usr/local/bin/supercronic; \
    apt-get purge -y --auto-remove curl

WORKDIR /app
COPY pyproject.toml README.md ./
COPY imap_to_papra ./imap_to_papra
RUN pip install --no-cache-dir .

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

RUN useradd --system --create-home --uid 10001 papra \
 && mkdir -p /var/lock/imap-to-papra \
 && chown -R papra:papra /var/lock/imap-to-papra
USER papra

# Configuration arrives as environment variables, so there is nothing to mount.
ENV CRON_SCHEDULE="*/5 * * * *" \
    PYTHONUNBUFFERED=1

# Overridable: `docker run --rm <image> imap-to-papra --check` works.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["supercronic", "/tmp/crontab"]
