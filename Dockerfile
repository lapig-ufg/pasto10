FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# git is required for uv to fetch semente-agents from the lapig-ufg/semente
# repository; ffmpeg is required by the TTS feature (pydub).
RUN apt-get update && apt-get -y install git ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --no-cache