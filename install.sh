#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${LDL_MCP_REPO:-https://github.com/Gyoungwe/lacewing-digital-library-mcp.git}"
INSTALL_DIR="${LDL_MCP_HOME:-$HOME/.local/share/lacewing-digital-library-mcp}"
CODEX_HOME_DIR="${CODEX_HOME:-$HOME/.codex}"
SKILL_DIR="$CODEX_HOME_DIR/skills/lacewing-digital-library"

command -v git >/dev/null || { echo "git is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
command -v codex >/dev/null || { echo "codex CLI is required" >&2; exit 1; }

mkdir -p "$(dirname "$INSTALL_DIR")" "$CODEX_HOME_DIR/skills"
if [ -d "$INSTALL_DIR/.git" ]; then
  git -C "$INSTALL_DIR" pull --ff-only
else
  rm -rf "$INSTALL_DIR"
  git clone "$REPO_URL" "$INSTALL_DIR"
fi

python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip >/dev/null
"$INSTALL_DIR/.venv/bin/python" -m pip install -e "$INSTALL_DIR" >/dev/null

rm -rf "$SKILL_DIR"
cp -R "$INSTALL_DIR/skill" "$SKILL_DIR"

codex mcp remove lacewing-digital-library >/dev/null 2>&1 || true
codex mcp add lacewing-digital-library -- "$INSTALL_DIR/.venv/bin/python" -m ldl_mcp

echo "Installed Lacewing Digital Library MCP + Codex skill."
echo "MCP: lacewing-digital-library"
echo "Skill: $SKILL_DIR"
echo "Try: Use \$lacewing-digital-library to resolve Mantispa styriaca."
