#!/usr/bin/env bash
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
runtime="$root/.runtime"
pen="$runtime/node_modules/.bin/pen"
client="${1:-help}"

fail() { printf '%s\n' "$*" >&2; exit 1; }

case "$client" in
  help|-h|--help)
    printf '%s\n' '用法：bash setup_local_pen.sh claude|codex|both|check|login|desktop'
    exit 0
    ;;
  desktop)
    [[ "$(uname -s)" == Darwin ]] || fail 'Pen 窗口自动重开仅支持 macOS。'
    if [[ ! -x "$root/pen-reopen" ]]; then
      command -v swiftc >/dev/null || fail '请先安装 Xcode 命令行工具。'
      swiftc -warnings-as-errors -o "$root/pen-reopen" "$root/pen_reopen.swift"
    fi
    printf '请在系统设置的「隐私与安全性 → 辅助功能」里添加并开启此文件：\n%s\n' "$root/pen-reopen"
    exit 0
    ;;
  check|login|claude|codex|both) ;;
  *) fail '参数无效。运行 bash setup_local_pen.sh --help 查看可用选项。' ;;
esac

python="$(command -v python3)" || fail '请先安装 Python 3.10 或更新版本。'
node="$(command -v node)" || fail '请先安装 Node.js 22.19 或更新版本。'
"$python" -c 'import sys; sys.exit(sys.version_info < (3, 10))' || fail 'Python 版本需至少为 3.10。'
"$node" -e 'const [major,minor]=process.versions.node.split(".").map(Number); process.exit(major<22||(major===22&&minor<19)?1:0)' \
  || fail 'Node.js 版本需至少为 22.19。'

if [[ "$client" == check ]]; then
  "$python" --version
  "$node" --version
  [[ -x "$pen" ]] || fail '尚未安装渲染工具。请选择 claude、codex 或 both 完成接入。'
  "$node" "$pen" version
  exit 0
fi
if [[ "$client" == login ]]; then
  [[ -x "$pen" ]] || fail '请先完成安装，再运行 login。'
  exec "$node" "$pen" login
fi
if [[ "$client" == claude || "$client" == both ]]; then
  command -v claude >/dev/null || fail '找不到 claude，请先安装 Claude Code。'
fi
if [[ "$client" == codex || "$client" == both ]]; then
  command -v codex >/dev/null || fail '找不到 codex，请先安装 Codex CLI。'
fi

if [[ ! -x "$pen" ]]; then
  command -v npm >/dev/null || fail '找不到 npm，请检查 Node.js 安装。'
  npm install --prefix "$runtime" --save-exact --ignore-scripts --no-fund --no-audit '@pen.dev/cli@0.3.16'
fi

if [[ "$client" == claude || "$client" == both ]]; then
  if claude mcp get local_pen >/dev/null 2>&1; then
    printf '%s\n' 'Claude Code 已有 local_pen 注册，已保留。升级步骤见 README。'
  else
    claude mcp add --scope user --transport stdio local_pen \
      -e "LOCAL_PEN_CLI=$pen" -e "LOCAL_PEN_NODE=$node" -- "$python" -B "$root/local_pen_mcp.py"
  fi
fi

if [[ "$client" == codex || "$client" == both ]]; then
  if codex mcp get local_pen >/dev/null 2>&1; then
    printf '%s\n' 'Codex 已有 local_pen 注册，已保留。升级步骤见 README。'
  else
    codex mcp add local_pen --env "LOCAL_PEN_CLI=$pen" --env "LOCAL_PEN_NODE=$node" -- "$python" -B "$root/local_pen_mcp.py"
  fi
fi

printf '%s\n' '重新打开你的 AI 客户端，然后按 README 里的提示词体验。'
