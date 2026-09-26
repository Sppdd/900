#!/usr/bin/env bash
# Build the sandcoder website container and run it as a Nebius Serverless AI Endpoint.
#
# Prereqs: Docker running, `nebius` CLI logged in (`nebius profile create`), and a
# Nebius AI Cloud project. Note: this is your *cloud* project id (project-e00...),
# which is different from the Token Factory project id used for Sandboxes.
#
#   NEBIUS_CLOUD_PROJECT_ID=project-e00... ./deploy/deploy-site.sh          # deploy
#   DRY_RUN=1 NEBIUS_CLOUD_PROJECT_ID=project-e00... ./deploy/deploy-site.sh
#
# IMPORTANT: `nebius ai endpoint create` defaults to an H100 GPU platform. A static
# site needs CPU only, so PLATFORM/PRESET are pinned below; check the values your
# region offers (`nebius compute platform list`) and adjust if needed.
set -euo pipefail

NEBIUS=${NEBIUS:-nebius}
PROJECT_ID=${NEBIUS_CLOUD_PROJECT_ID:?set NEBIUS_CLOUD_PROJECT_ID (your Nebius AI Cloud project id)}
REGION=${REGION:-eu-north1}
REGISTRY_NAME=${REGISTRY_NAME:-sandcoder}
ENDPOINT_NAME=${ENDPOINT_NAME:-sandcoder-site}
PLATFORM=${PLATFORM:-cpu-d3}
PRESET=${PRESET:-2vcpu-8gb}
DRY_RUN=${DRY_RUN:-0}
HERE=$(cd "$(dirname "$0")/.." && pwd)
TAG=site-$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || date +%s)

json_field() { python3 -c "import json,sys; d=json.load(sys.stdin); print(eval('d'+sys.argv[1]))" "$1"; }

echo "==> registry '$REGISTRY_NAME' in $PROJECT_ID"
if ! REG_JSON=$($NEBIUS registry get-by-name --parent-id "$PROJECT_ID" --name "$REGISTRY_NAME" --format json 2>/dev/null); then
  [ "$DRY_RUN" = 1 ] && { echo "(dry run) would create registry $REGISTRY_NAME"; REG_JSON='{"metadata":{"id":"registry-dryrun"}}'; } \
    || REG_JSON=$($NEBIUS registry create --parent-id "$PROJECT_ID" --name "$REGISTRY_NAME" --format json)
fi
REGISTRY_ID=$(echo "$REG_JSON" | json_field "['metadata']['id']")
IMAGE="cr.${REGION}.nebius.cloud/${REGISTRY_ID#registry-}/sandcoder-site:${TAG}"
echo "    image: $IMAGE"

echo "==> build (linux/amd64) and push"
if [ "$DRY_RUN" = 1 ]; then
  echo "(dry run) docker buildx build --platform linux/amd64 -t $IMAGE --push $HERE/site"
else
  $NEBIUS registry configure-helper >/dev/null
  docker buildx build --platform linux/amd64 -t "$IMAGE" --push "$HERE/site"
fi

echo "==> endpoint '$ENDPOINT_NAME' ($PLATFORM / $PRESET, public, no auth)"
if $NEBIUS ai endpoint get-by-name --parent-id "$PROJECT_ID" --name "$ENDPOINT_NAME" --format json >/dev/null 2>&1; then
  echo "Endpoint $ENDPOINT_NAME already exists. Delete it first or set ENDPOINT_NAME=... to deploy a new one." >&2
  exit 1
fi
ARGS=(ai endpoint create --parent-id "$PROJECT_ID" --name "$ENDPOINT_NAME" --image "$IMAGE"
      --container-port 8080 --platform "$PLATFORM" --preset "$PRESET" --auth none)
[ "$DRY_RUN" = 1 ] && ARGS+=(--dry-run)
$NEBIUS "${ARGS[@]}"

[ "$DRY_RUN" = 1 ] && exit 0
echo "==> public URL"
$NEBIUS ai endpoint get-by-name --parent-id "$PROJECT_ID" --name "$ENDPOINT_NAME" --format json \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps(d.get('status',{}).get('public_endpoints', d.get('status',{})), indent=2))"
