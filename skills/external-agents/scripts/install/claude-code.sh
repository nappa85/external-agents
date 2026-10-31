#!/usr/bin/env bash
# Installs the pinned Claude Code CLI through npm. Idempotent; safe to rerun from a setup
# hook. An installed CLI of another version is kept unless EXTERNAL_AGENTS_PIN=1.
#
# npm verifies the package against the registry's integrity hash. To upgrade, change the
# version, for example to `npm view @anthropic-ai/claude-code version`.
set -euo pipefail

version="2.1.308"

installed="$(claude --version 2>/dev/null | cut -d ' ' -f 1 || true)"
if [ "$installed" = "$version" ]; then
  echo "Claude Code $version is already installed"
elif [ -n "$installed" ] && [ "${EXTERNAL_AGENTS_PIN:-}" != 1 ]; then
  echo "Claude Code $installed is installed; keeping it (EXTERNAL_AGENTS_PIN=1 installs $version)"
else
  if ! command -v npm >/dev/null 2>&1; then
    echo "Claude Code install: npm (Node.js 18 or later) is required" >&2
    exit 1
  fi
  echo "Installing Claude Code $version..."
  npm install --global --no-fund --no-audit "@anthropic-ai/claude-code@$version"
fi

if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] &&
  [ ! -s "$HOME/.claude/.credentials.json" ]; then
  cat <<'MSG'
Claude Code has no credentials. Run `claude setup-token` and export
CLAUDE_CODE_OAUTH_TOKEN, or export ANTHROPIC_API_KEY.
MSG
fi
