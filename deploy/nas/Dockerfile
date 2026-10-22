FROM python:3.12-slim-bookworm

# Keep the uv toolchain consistent with the other NAS services in nasopencode.
COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /uvx /usr/local/bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH=/opt/venv/bin:$PATH \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY deploy/nas/requirements-video.txt /tmp/requirements-video.txt
RUN uv venv /opt/venv --python 3.12 \
    && uv pip install --python /opt/venv/bin/python -r /tmp/requirements-video.txt \
    && /opt/venv/bin/playwright install --with-deps chromium \
    && rm -f /tmp/requirements-video.txt

COPY app ./app
COPY README.md pyproject.toml ./
COPY batch_download_huitun.py login_douyin.py verify_douyin_interactive.py ./
COPY deploy/nas/docker-entrypoint.sh /usr/local/bin/douyin-entrypoint
RUN uv pip install --python /opt/venv/bin/python --no-deps . \
    && chmod 0755 /usr/local/bin/douyin-entrypoint

ENTRYPOINT ["/usr/local/bin/douyin-entrypoint"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
