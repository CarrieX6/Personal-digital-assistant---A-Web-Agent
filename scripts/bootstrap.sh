#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "$0")/.." && pwd)"
install_system_deps=false
plan_only=false
deploy_args=()

for argument in "$@"; do
  case "$argument" in
    --install-system-deps) install_system_deps=true ;;
    --plan) plan_only=true ;;
    *) deploy_args+=("$argument") ;;
  esac
done

prepend_if_directory() {
  if [[ -d "$1" ]]; then
    PATH="$1:$PATH"
  fi
}

if command -v brew >/dev/null 2>&1; then
  brew_bin="$(command -v brew)"
  node_prefix="$($brew_bin --prefix node@22 2>/dev/null || true)"
  python_prefix="$($brew_bin --prefix python@3.12 2>/dev/null || true)"
  prepend_if_directory "$node_prefix/bin"
  prepend_if_directory "$python_prefix/bin"
  prepend_if_directory "$python_prefix/libexec/bin"
fi
export PATH

python_bin=""
for candidate in "$project_root/.venv/bin/python" python3.12 python3; do
  if [[ "$candidate" == */* ]]; then
    [[ -x "$candidate" ]] || continue
    resolved="$candidate"
  else
    resolved="$(command -v "$candidate" 2>/dev/null || true)"
    [[ -n "$resolved" ]] || continue
  fi
  if "$resolved" -c 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 14) else 1)' >/dev/null 2>&1; then
    python_bin="$resolved"
    break
  fi
done

node_ok=false
if command -v node >/dev/null 2>&1; then
  if node -e 'const [a,b]=process.versions.node.split(".").map(Number); process.exit(a>22 || (a===22 && b>=13) ? 0 : 1)' >/dev/null 2>&1; then
    node_ok=true
  fi
fi

missing=()
brew_formulas=()
if [[ -z "$python_bin" ]]; then
  missing+=("Python 3.11–3.13（推荐 3.12）")
  brew_formulas+=("python@3.12")
fi
if ! $node_ok; then
  missing+=("Node.js 22.13+（含 Corepack）")
  brew_formulas+=("node@22")
fi
if ! command -v git >/dev/null 2>&1; then
  missing+=("Git")
  brew_formulas+=("git")
fi

if (( ${#missing[@]} > 0 )); then
  echo "检测到缺失或版本不兼容的运行环境："
  for item in "${missing[@]}"; do echo "- $item"; done
  if [[ "$(uname -s)" == "Darwin" ]]; then
    if ! command -v brew >/dev/null 2>&1; then
      echo "请先打开 https://brew.sh/，按 Homebrew 官方说明安装，再重新运行本脚本。"
      exit 2
    fi
    echo "推荐修复命令："
    echo "  $brew_bin install ${brew_formulas[*]}"
    if $plan_only; then exit 0; fi
    if ! $install_system_deps; then
      echo "若要由本脚本执行这条命令，请重新运行："
      echo "  ./scripts/bootstrap.sh --install-system-deps ${deploy_args[*]-}"
      exit 2
    fi
    "$brew_bin" install "${brew_formulas[@]}"
    node_prefix="$("$brew_bin" --prefix node@22 2>/dev/null || true)"
    python_prefix="$("$brew_bin" --prefix python@3.12 2>/dev/null || true)"
    PATH="$node_prefix/bin:$python_prefix/bin:$python_prefix/libexec/bin:$PATH"
    export PATH
    python_bin="$(command -v python3.12 || command -v python3)"
  else
    echo "当前系统不自动修改系统包。请按 docs/guides/installation.md 的对应平台步骤安装后重试。"
    exit 2
  fi
elif $plan_only; then
  echo "运行环境已满足：$($python_bin --version 2>&1)，$(node --version)，$(git --version)。"
  exit 0
fi

cd "$project_root"
"$python_bin" scripts/deploy.py install "${deploy_args[@]}"
