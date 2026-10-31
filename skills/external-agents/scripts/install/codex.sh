#!/usr/bin/env bash
# Installs the pinned Codex CLI through npm. Idempotent; safe to rerun from a setup hook.
# An installed CLI of another version is kept unless EXTERNAL_AGENTS_PIN=1.
#
# npm verifies the package against the registry's integrity hash. To upgrade, change the
# version, for example to `npm view @openai/codex version`.
set -euo pipefail

version="0.163.0"

installed="$(codex --version 2>/dev/null | awk '{print $NF}' || true)"
if [ "$installed" = "$version" ]; then
  echo "Codex $version is already installed"
elif [ -n "$installed" ] && [ "${EXTERNAL_AGENTS_PIN:-}" != 1 ]; then
  echo "Codex $installed is installed; keeping it (EXTERNAL_AGENTS_PIN=1 installs $version)"
else
  if ! command -v npm >/dev/null 2>&1; then
    echo "Codex install: npm (Node.js 18 or later) is required" >&2
    exit 1
  fi
  echo "Installing Codex $version..."
  npm install --global --no-fund --no-audit "@openai/codex@$version"
fi

if [ -z "${OPENAI_API_KEY:-}" ] && [ ! -s "${CODEX_HOME:-$HOME/.codex}/auth.json" ]; then
  cat <<'MSG'
Codex has no credentials. Run `codex login`, or export OPENAI_API_KEY.
MSG
fi
