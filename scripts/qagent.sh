#!/usr/bin/env bash
# 项目内 qagent 启动器（无需 pip install 到 PATH）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m qagent.cli "$@"
