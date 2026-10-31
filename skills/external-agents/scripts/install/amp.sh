#!/usr/bin/env bash
# Installs the pinned Amp CLI through npm. Idempotent; safe to rerun from a setup hook.
# An installed CLI of another version is kept unless EXTERNAL_AGENTS_PIN=1.
#
# npm verifies the package against the registry's integrity hash. To upgrade, change the
# version, for example to `npm view @sourcegraph/amp version`.
set -euo pipefail

version="0.0.1792185362-ga97f29"

installed="$(amp --version 2>/dev/null | awk '{print $1}' || true)"
if [ "$installed" = "$version" ]; then
  echo "Amp $version is already installed"
elif [ -n "$installed" ] && [ "${EXTERNAL_AGENTS_PIN:-}" != 1 ]; then
  echo "Amp $installed is installed; keeping it (EXTERNAL_AGENTS_PIN=1 installs $version)"
else
  if ! command -v npm >/dev/null 2>&1; then
    echo "Amp install: npm (Node.js 18 or later) is required" >&2
    exit 1
  fi
  echo "Installing Amp $version..."
  npm install --global --no-fund --no-audit "@sourcegraph/amp@$version"
fi

if [ -z "${AMP_API_KEY:-}" ] && [ ! -s "$HOME/.local/share/amp/secrets.json" ]; then
  cat <<'MSG'
Amp has no credentials. Run `amp login`, or export AMP_API_KEY.
MSG
fi
