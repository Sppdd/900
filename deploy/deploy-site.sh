#!/usr/bin/env bash
# Deploy the sandcoder website (site/) to Google Cloud Run.
#
#   GCP_PROJECT=my-project ./deploy/deploy-site.sh
#   GCP_PROJECT=my-project REGION=europe-west1 SERVICE=sandcoder ./deploy/deploy-site.sh
#
# Uses Cloud Build from source (site/Dockerfile), so no local Docker is needed.
# Scales to zero (min-instances 0), tiny footprint: a static nginx site.
set -euo pipefail

PROJECT=${GCP_PROJECT:?set GCP_PROJECT to your Google Cloud project id}
REGION=${REGION:-us-central1}
SERVICE=${SERVICE:-sandcoder-site}
HERE=$(cd "$(dirname "$0")/.." && pwd)

echo "==> enabling Cloud Run, Cloud Build and Artifact Registry APIs in $PROJECT"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com --project "$PROJECT"

echo "==> deploying $SERVICE to $REGION"
gcloud run deploy "$SERVICE" \
  --project "$PROJECT" --region "$REGION" \
  --source "$HERE/site" \
  --port 8080 --cpu 1 --memory 256Mi \
  --min-instances 0 --max-instances 3 \
  --allow-unauthenticated \
  --quiet

URL=$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format 'value(status.url)')
echo "==> live at $URL"
curl -fsS -o /dev/null -w "health: %{http_code}\n" "$URL/healthz" || true
