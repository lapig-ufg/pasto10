# The dependency base image (Python, uv, system packages and /app/.venv) is
# built once from Dockerfile.base. Rebuild it only when pyproject.toml or
# uv.lock changes:
#
#   docker build -f Dockerfile.base -t lapig/pasto10:base .
#
# Code-only changes then rebuild quickly, reusing the installed virtualenv.
ARG BASE_IMAGE=lapig/pasto10:base
FROM ${BASE_IMAGE}

WORKDIR /app

COPY . .
