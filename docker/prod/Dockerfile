FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# git is required for uv to fetch semente-agents from the lapig-ufg/semente
# repository; ffmpeg is required by the TTS feature (pydub) and by the biomass
# video tool; libarchive-tools provides bsdtar, the extractor rarfile uses for
# .rar uploads; libpq-dev/gcc support PostgreSQL drivers if added later.
RUN apt-get update && apt-get -y install libpq-dev gcc git htop curl ffmpeg libarchive-tools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH"

# uv.lock pins semente-agents to the lapig-ufg/semente feature branch: uv sync
# fetches it from the repository. git (above) is required for that fetch.
COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --no-cache --no-install-project

COPY . .

RUN uv sync --frozen --no-cache

EXPOSE 8501

CMD ["streamlit", "run", "app/streamlit_app.py", "--server.port", "8501", "--server.address", "0.0.0.0"]
