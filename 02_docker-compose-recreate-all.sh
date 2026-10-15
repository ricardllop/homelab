#!/bin/bash

set -e

# Like 01, but recreates every container even if nothing changed (full restart + new images + config changes)
for compose_file in */docker-compose.yml; do
  dir=$(dirname "$compose_file")
  echo "🔄 Pulling and recreating: $dir"
  (
    cd "$dir"
    docker compose up -d --pull always --force-recreate
  )
done
