#!/usr/bin/env bash
# Fetch the etcd binaries the claim path ships to control-plane nodes.
#
# claim.py copies etcd/etcdctl/etcdutl to every CP during the private-apiserver
# window (staged etcd, then the etcdutl restore). They are 62 MB of linux-amd64
# ELF and are deliberately NOT committed; run this once per checkout. The
# version is pinned to what the frozen templates were built with
# (build_template.sh ETCD_VERSION) - a mismatch corrupts the restored member.
set -euo pipefail
ETCD_VERSION="${ETCD_VERSION:-3.5.24}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARBALL="etcd-v${ETCD_VERSION}-linux-amd64.tar.gz"
URL="https://github.com/etcd-io/etcd/releases/download/v${ETCD_VERSION}/${TARBALL}"

if [ -x "$HERE/etcd" ] && [ -x "$HERE/etcdctl" ] && [ -x "$HERE/etcdutl" ] && [ -z "${FORCE:-}" ]; then
  echo "etcd binaries present in $HERE (FORCE=1 to refetch)"; exit 0
fi
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
echo "fetching $URL"
curl -fsSL --retry 3 -o "$tmp/$TARBALL" "$URL"
tar -xzf "$tmp/$TARBALL" -C "$tmp"
for b in etcd etcdctl etcdutl; do
  install -m 0755 "$tmp/etcd-v${ETCD_VERSION}-linux-amd64/$b" "$HERE/$b"
done
echo "installed etcd v${ETCD_VERSION} (etcd, etcdctl, etcdutl) into $HERE"
