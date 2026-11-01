FROM python:3.13-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends rclone restic ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY backup_on_sight.py ./
RUN pip install --no-cache-dir .
COPY docker-entrypoint.sh /usr/local/bin/
ENV BACKUP_ON_SIGHT_CONFIG=/config/config.yaml
# An arbitrary EUID has no home directory, and restic and rclone keep their cache and config there.
ENV HOME=/state/.home
ENTRYPOINT ["tini", "--", "docker-entrypoint.sh"]
CMD ["bos", "daemon"]
