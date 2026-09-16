#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "$0")" && pwd)"
cd "$project_root"
echo "个人数字助手将检查 Python、Node.js 与 Git，并安装项目基础运行环境。"
echo "模型与许可证稍后在 Web 的“设置与模型安装”中单独确认。"
read -r -p "是否继续？[y/N] " answer
if [[ "$answer" != "y" && "$answer" != "Y" ]]; then
  echo "已取消。"
  exit 0
fi
exec ./scripts/quickstart.sh --install-system-deps
