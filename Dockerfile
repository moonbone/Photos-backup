FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends libimage-exiftool-perl ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY photovault ./photovault
RUN pip install --no-cache-dir .

ENV PHOTOVAULT_CONFIG=/config/config.toml
VOLUME ["/config", "/data"]
EXPOSE 8420

CMD ["photovault", "serve", "--host", "0.0.0.0"]
