#!/usr/bin/env bash
# Installs the pinned Mistral Vibe CLI with `uv tool install`. Idempotent; safe to rerun
# from a setup hook. An installed CLI of another version is kept unless EXTERNAL_AGENTS_PIN=1.
#
# The package comes from PyPI over HTTPS, as Vibe's own installer does, without running a
# remote install script. To upgrade, change the version, for example to
# `curl -fsSL https://pypi.org/pypi/mistral-vibe/json | jq -r .info.version`.
set -euo pipefail

version="3.1.4"

installed="$(vibe --version 2>/dev/null | awk '{print $2}' || true)"
if [ "$installed" = "$version" ]; then
  echo "Vibe $version is already installed"
elif [ -n "$installed" ] && [ "${EXTERNAL_AGENTS_PIN:-}" != 1 ]; then
  echo "Vibe $installed is installed; keeping it (EXTERNAL_AGENTS_PIN=1 installs $version)"
else
  if ! command -v uv >/dev/null 2>&1; then
    echo "Vibe install: uv is required (https://docs.astral.sh/uv/)" >&2
    exit 1
  fi
  echo "Installing Vibe $version..."
  # Vibe's documented interpreter: some dependencies ship no wheels for newer Pythons.
  uv tool install --force --python 3.14 "mistral-vibe==$version"
  case ":$PATH:" in *":$(uv tool dir --bin):"*) ;; *) echo "note: add $(uv tool dir --bin) to PATH" ;; esac
fi

if [ -z "${MISTRAL_API_KEY:-}" ] && ! grep -qs '^MISTRAL_API_KEY=' "${VIBE_HOME:-$HOME/.vibe}/.env"; then
  cat <<'MSG'
Vibe has no MISTRAL_API_KEY in the environment or in ~/.vibe/.env. Export it, or save it
to ~/.vibe/.env as MISTRAL_API_KEY=... A key that `vibe --setup` keeps in the system
keyring is not used: reviewers and the coding sandbox run without the session bus.
MSG
fi
