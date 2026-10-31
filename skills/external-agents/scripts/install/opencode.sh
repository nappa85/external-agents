#!/usr/bin/env bash
# Installs the pinned opencode CLI. Idempotent; safe to rerun from a setup hook.
# An installed CLI of another version is kept unless EXTERNAL_AGENTS_PIN=1.
#
# The release archive is downloaded directly and checked against a committed SHA-256, so
# no remote install script runs. To upgrade, change the version and the checksums from the
# release's asset digests, for example with
# `gh api repos/anomalyco/opencode/releases/tags/v<version> --jq '.assets[] | [.name, .digest]'`.
set -euo pipefail

version="1.19.3"
install_dir="$HOME/.opencode/bin"
link_dir="$HOME/.local/bin"

current="$(opencode --version 2>/dev/null || true)"
if [ "$current" = "$version" ]; then
  echo "opencode $version is already installed"
  exit 0
elif [ -n "$current" ] && [ "${EXTERNAL_AGENTS_PIN:-}" != 1 ]; then
  echo "opencode $current is installed; keeping it (EXTERNAL_AGENTS_PIN=1 installs $version)"
  exit 0
fi

# The baseline (no AVX2) and musl builds are not pinned; those hosts are unsupported.
case "$(uname -s)-$(uname -m)" in
  Linux-x86_64)
    asset="opencode-linux-x64.tar.gz"
    sha256="13a8fb2978302071b86f9d7e00da9c8895f9cd846978379c3bdc6a5333536c9b"
    ;;
  Linux-aarch64 | Linux-arm64)
    asset="opencode-linux-arm64.tar.gz"
    sha256="7900818a6a98881edbbadab4b7525940eff3b9750040c200481f9f56cacee405"
    ;;
  Darwin-x86_64)
    asset="opencode-darwin-x64.zip"
    sha256="4e29dbe2f61ace728370523c88027d0742b3a79edcdd2c70e404f616d4ef8f36"
    ;;
  Darwin-arm64)
    asset="opencode-darwin-arm64.zip"
    sha256="c2d0fecd27acf6e08be02c1f5d384614d07495587766bcc4a6bff3971749bb64"
    ;;
  *)
    echo "opencode install: unsupported platform $(uname -s)-$(uname -m)" >&2
    exit 1
    ;;
esac

installed="$("$install_dir/opencode" --version 2>/dev/null || true)"
if [ "$installed" != "$version" ]; then
  echo "Installing opencode $version..."
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT
  curl --proto '=https' --tlsv1.2 -fsSL \
    "https://github.com/anomalyco/opencode/releases/download/v$version/$asset" -o "$work/$asset"
  if command -v sha256sum >/dev/null 2>&1; then
    actual="$(sha256sum "$work/$asset" | cut -d ' ' -f 1)"
  else
    actual="$(shasum -a 256 "$work/$asset" | cut -d ' ' -f 1)"
  fi
  if [ "$actual" != "$sha256" ]; then
    echo "opencode install: checksum mismatch for $asset (expected $sha256, got $actual)" >&2
    exit 1
  fi
  case "$asset" in
    *.tar.gz) tar -xzf "$work/$asset" -C "$work" ;;
    *.zip) unzip -q "$work/$asset" -d "$work" ;;
  esac
  mkdir -p "$install_dir"
  # Install under a temporary name, then rename, so a failed copy never leaves a
  # truncated binary that a later run would mistake for a finished install.
  cp "$work/opencode" "$install_dir/opencode.new"
  chmod 755 "$install_dir/opencode.new"
  mv -f "$install_dir/opencode.new" "$install_dir/opencode"
fi

mkdir -p "$link_dir"
ln -sfn "$install_dir/opencode" "$link_dir/opencode"
case ":$PATH:" in *":$link_dir:"*) ;; *) echo "note: add $link_dir to PATH" ;; esac

# Saved logins and provider environment variables are listed separately; only
# "0 credentials" with no environment section means nothing is usable.
auth="$("$link_dir/opencode" auth list 2>&1 || true)"
if grep -q ' 0 credentials' <<<"$auth" && ! grep -q 'Environment' <<<"$auth"; then
  cat <<'MSG'
opencode has no provider credentials. Run `opencode auth login`, or export a provider
key such as OPENAI_API_KEY.
MSG
fi
