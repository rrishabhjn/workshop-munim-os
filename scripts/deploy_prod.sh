#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

SKIP_TESTS=0
DEPLOY_FIREBASE=0
for arg in "$@"; do
  case "$arg" in
    --skip-tests) SKIP_TESTS=1 ;;
    --with-firebase) DEPLOY_FIREBASE=1 ;;
    *)
      echo "Unknown option: $arg" >&2
      echo "Usage: $0 [--skip-tests] [--with-firebase]" >&2
      exit 2
      ;;
  esac
done

if [[ ! -f .env ]]; then
  echo "ERROR: .env not found in $ROOT_DIR" >&2
  exit 1
fi

PYTHON_BIN="${ROOT_DIR}/backend/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3 || command -v python)"
fi

# Extract only non-secret build/deploy identifiers safely without sourcing .env
eval "$("$PYTHON_BIN" - <<'PY'
import shlex
from dotenv import dotenv_values

vals = dotenv_values(".env")
keys = {
    "SUPABASE_URL": vals.get("SUPABASE_URL") or "",
    "SUPABASE_PUBLISHABLE_KEY": vals.get("SUPABASE_PUBLISHABLE_KEY") or "",
    "GCP_PROJECT_ID": vals.get("GCP_PROJECT_ID") or "workshop-munim-os-514807",
    "GCP_REGION": vals.get("GCP_REGION") or "asia-south1",
}
for k, v in keys.items():
    print(f"{k}={shlex.quote(v)}")
PY
)"

if [[ -z "$SUPABASE_URL" || -z "$SUPABASE_PUBLISHABLE_KEY" ]]; then
  echo "ERROR: SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY must be set in .env" >&2
  exit 1
fi

if [[ "$SKIP_TESTS" -eq 0 ]]; then
  echo "==> Running backend pytest suite..."
  "$PYTHON_BIN" -m pytest -c backend/pytest.ini backend/tests -q
fi

echo "==> Building container image via Cloud Build ($GCP_PROJECT_ID / $GCP_REGION)..."
gcloud builds submit \
  --project "$GCP_PROJECT_ID" \
  --config cloudbuild.yaml \
  --substitutions="_VITE_SUPABASE_URL=${SUPABASE_URL},_VITE_SUPABASE_PUBLISHABLE_KEY=${SUPABASE_PUBLISHABLE_KEY}" \
  .

echo "==> Deploying workshop-munim-os to Cloud Run ($GCP_REGION)..."
gcloud run deploy workshop-munim-os \
  --project "$GCP_PROJECT_ID" \
  --region "$GCP_REGION" \
  --image "asia-south1-docker.pkg.dev/${GCP_PROJECT_ID}/workshop-munim/app:latest" \
  --allow-unauthenticated

if [[ "$DEPLOY_FIREBASE" -eq 1 ]]; then
  echo "==> Deploying Firebase Hosting (tfc-admin)..."
  firebase deploy --only hosting
fi

echo "==> Deploy complete."
