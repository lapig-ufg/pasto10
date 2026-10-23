#!/usr/bin/env bash
# Run the Pasto10 Streamlit chat locally (SQLite, no production services).
# Starts app/streamlit_app.py (the Semente chat plus the test-property picker).
# PORT overrides the default port 8501.
#
# Usage: scripts/run_local.sh [path/to/env/file]
#
# Only GOOGLE_API_KEY and GEE_* are read from the env file (default: ./.env).
# Databases, S3 and WhatsApp settings are never loaded, so a file shared with
# another project cannot make this app touch that project's services.
set -euo pipefail

cd "$(dirname "$0")/.."
ENV_FILE="${1:-.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Env file not found: $ENV_FILE (see .env.example)" >&2
  exit 1
fi

read_var() {
  grep -E "^$1=" "$ENV_FILE" | tail -n1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//'
}

export GOOGLE_API_KEY="$(read_var GOOGLE_API_KEY)"
export GEE_PROJECT="$(read_var GEE_PROJECT)"
export GEE_SERVICE_ACCOUNT="$(read_var GEE_SERVICE_ACCOUNT)"
key_file="$(read_var GEE_KEY_FILE)"
if [[ -n "$key_file" && "$key_file" != /* ]]; then
  key_file="$(cd "$(dirname "$ENV_FILE")" && pwd)/$key_file"
fi
export GEE_KEY_FILE="$key_file"

export APP_ENV=development
export DATABASE_TYPE=sqlite
export PYTHONPATH="$PWD"

export SEMENTE_MANIFEST=semente.yaml

exec uv run streamlit run app/streamlit_app.py --server.port "${PORT:-8501}" "${@:2}"
