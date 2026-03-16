FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl nodejs npm \
    && npm install -g @openai/codex @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY nimble_reviewer ./nimble_reviewer

RUN pip install .

RUN useradd --create-home --shell /usr/sbin/nologin reviewer
ENV HOME=/home/reviewer
RUN mkdir -p /data /cache/repos /home/reviewer/.codex /home/reviewer/.claude \
    && chown -R reviewer:reviewer /home/reviewer /app /data /cache

USER reviewer

EXPOSE 8080

CMD ["python", "-m", "nimble_reviewer"]
