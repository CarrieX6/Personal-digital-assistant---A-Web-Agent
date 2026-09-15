#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "$0")/.." && pwd)"
exec "$project_root/scripts/bootstrap.sh" "$@"
