#!/usr/bin/env bash
# 兼容旧入口：转到跨平台 Python 安装脚本
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec python3 "$ROOT/scripts/install_skill.py" "$@"
