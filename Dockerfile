FROM python:3.13-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends rclone restic ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY backup_on_sight.py ./
RUN pip install --no-cache-dir .
ENV BACKUP_ON_SIGHT_CONFIG=/config/config.yaml
CMD ["bos", "daemon"]
