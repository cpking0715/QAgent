#!/usr/bin/env bash
# 安装 QAgent 技能到 Cursor，并安装 Python 包
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SKILLS_SRC="$ROOT/skills"
TARGET="${1:-.cursor/skills}"

mkdir -p "$TARGET"
for skill in qa-orchestrator qa-test-design qa-testcase-generator; do
  rm -rf "$TARGET/$skill"
  cp -R "$SKILLS_SRC/$skill" "$TARGET/$skill"
  echo "已安装: $TARGET/$skill"
done

if command -v pip >/dev/null 2>&1; then
  pip install -e "$ROOT"
  echo "已安装 qagent CLI: qagent --help"
else
  echo "警告: 未找到 pip，请手动运行: pip install -e $ROOT"
fi

echo ""
echo "验证命令:"
echo "  qagent pipeline status"
echo "  qagent validate output/testcases.md --plan output/test-plan.md"
