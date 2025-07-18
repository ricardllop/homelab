#!/bin/bash

set -e

# Find all docker-compose.yml files in subdirectories
for compose_file in */docker-compose.yml; do
  dir=$(dirname "$compose_file")
  echo "🟢 Starting Docker Compose in: $dir"
  (
    cd "$dir"
    docker compose up -d --pull always
  )
done
