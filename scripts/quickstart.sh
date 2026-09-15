#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "$0")/.." && pwd)"
arguments=("$@")
has_profile=false
has_photo_style=false
for argument in "${arguments[@]}"; do
  [[ "$argument" == "--profile" || "$argument" == --profile=* ]] && has_profile=true
  [[ "$argument" == "--photo-style" || "$argument" == --photo-style=* ]] && has_photo_style=true
done
$has_profile || arguments+=("--profile" "core")
$has_photo_style || arguments+=("--photo-style" "skip")

source "$project_root/scripts/bootstrap.sh" "${arguments[@]}"
cd "$project_root"
exec .venv/bin/python scripts/deploy.py start
