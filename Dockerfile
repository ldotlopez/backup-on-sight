FROM python:3.13-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends lftp rclone restic ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY backup_on_sight.py ./
RUN pip install --no-cache-dir .
COPY docker-entrypoint.sh /usr/local/bin/
ENTRYPOINT ["tini", "--", "docker-entrypoint.sh"]
CMD ["bos", "daemon"]
