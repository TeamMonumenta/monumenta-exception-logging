#!/usr/bin/env bash
set -euo pipefail

IMAGE="ghcr.io/teammonumenta/monumenta-automation/pr-bot"
TAG="${1:-latest}"

docker build -t "$IMAGE:$TAG" .
docker push "$IMAGE:$TAG"
