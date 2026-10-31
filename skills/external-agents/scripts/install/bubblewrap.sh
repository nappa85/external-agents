#!/usr/bin/env bash
# Installs bubblewrap, the coding sandbox, with the system package manager, then checks
# that it can create its namespaces. Idempotent; safe to rerun from a setup hook.
set -euo pipefail

if [ "$(uname -s)" != Linux ]; then
  echo "bubblewrap install: the coding sandbox needs Linux; $(uname -s) is unsupported" >&2
  exit 1
fi

sudo=()
if [ "$(id -u)" != 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    sudo=(sudo)
  else
    echo "bubblewrap install: needs root or sudo" >&2
    exit 1
  fi
fi

if ! command -v bwrap >/dev/null 2>&1; then
  echo "Installing bubblewrap..."
  if command -v apt-get >/dev/null 2>&1; then
    "${sudo[@]}" apt-get update
    "${sudo[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y bubblewrap
  elif command -v dnf >/dev/null 2>&1; then
    "${sudo[@]}" dnf install -y bubblewrap
  elif command -v zypper >/dev/null 2>&1; then
    "${sudo[@]}" zypper --non-interactive install bubblewrap
  elif command -v pacman >/dev/null 2>&1; then
    "${sudo[@]}" pacman -S --noconfirm --needed bubblewrap
  elif command -v apk >/dev/null 2>&1; then
    "${sudo[@]}" apk add bubblewrap
  elif command -v emerge >/dev/null 2>&1; then
    "${sudo[@]}" emerge --noreplace sys-apps/bubblewrap
  else
    echo "bubblewrap install: no supported package manager; install bubblewrap yourself" >&2
    exit 1
  fi
fi

if bwrap --unshare-all --ro-bind / / true 2>/dev/null; then
  echo "bubblewrap $(bwrap --version | awk '{print $2}') works"
  exit 0
fi

cat >&2 <<'MSG'
bubblewrap is installed but cannot create its namespaces here. Common causes:
- Inside a container: unprivileged user namespaces are usually blocked. Run the coding
  sandbox on the host or in a VM, or give the container the needed seccomp/AppArmor
  allowances.
- Ubuntu 23.10 or later: kernel.apparmor_restrict_unprivileged_userns=1 needs the
  bwrap-userns-restrict AppArmor profile that the apparmor package ships for /usr/bin/bwrap
  (`sudo apparmor_parser -r /etc/apparmor.d/bwrap-userns-restrict`).
- kernel.unprivileged_userns_clone=0 (older Debian) or user.max_user_namespaces=0.
MSG
exit 1
