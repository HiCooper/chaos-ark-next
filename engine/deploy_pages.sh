#!/usr/bin/env bash
# 【备用】把 site/ 发布到 GitHub Pages（gh-pages 分支）。
#
# 注意：2026-10-29 起 Pages 的 Source 改为 **GitHub Actions**，正常发布走
#   .github/workflows/pages.yml（push 到 main 且改动落在 site/ 即自动部署）。
# 本脚本保留给两种情况：Actions 不可用时的应急发布、或想临时切回分支部署。
#
# 为什么不直接让 Pages 读 main：
#   · GitHub Pages 的分支源只支持仓库根目录或 /docs，而本仓库的站点产物在 site/；
#   · /docs 已被设计文档占用，所以用独立的 gh-pages 分支承载产物。
#
# 用法：bash engine/deploy_pages.sh            # 发布当前 site/
#       bash engine/deploy_pages.sh --dry-run  # 只看会推什么，不推
#
# 前置（只需一次）：GitHub 仓库 Settings → Pages → Source = "Deploy from a branch"
#   → Branch = gh-pages / (root) → Save。之后每次跑本脚本即可更新站点。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/site"
BRANCH="gh-pages"
REMOTE="$(git -C "$ROOT" remote get-url origin)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

[ -f "$SRC/index.html" ] || { echo "✗ 找不到 $SRC/index.html，先构建前端并同步 site/"; exit 1; }

# 1:1 拷贝 site/（data + view + assets + index.html），并关掉 Jekyll 预处理
cp -R "$SRC/." "$TMP/"
rm -rf "$TMP/.git"
: > "$TMP/.nojekyll"

if [ "${1:-}" = "--dry-run" ]; then
  echo "将发布（$TMP）："
  find "$TMP" -maxdepth 1 -mindepth 1 | sed 's#^#  #'
  echo "  文件数：$(find "$TMP" -type f | wc -l | tr -d ' ')"
  du -sh "$TMP"
  exit 0
fi

cd "$TMP"
git init -q
git config user.name  "$(git -C "$ROOT" config user.name  || echo ark-deploy)"
git config user.email "$(git -C "$ROOT" config user.email || echo ark@local)"
git add -A
git commit -qm "deploy: $(date '+%Y-%m-%d %H:%M:%S') · $(git -C "$ROOT" rev-parse --short HEAD)@main"
git push -f "$REMOTE" "HEAD:$BRANCH" 2>&1 | tail -2

echo "✓ 已推送到 ${BRANCH}（$(find . -type f | wc -l | tr -d ' ') 个文件）"
echo "  站点：https://hicooper.github.io/chaos-ark-next/"
echo "  首次使用需在仓库 Settings → Pages 里把 Source 设为 Branch: ${BRANCH} / (root)"
