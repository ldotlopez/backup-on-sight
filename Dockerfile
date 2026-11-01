FROM python:3.13-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends rclone restic ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
ENV AUTO_NETWORK_BACKUP_CONFIG=/config/config.yaml
CMD ["anb", "daemon"]
