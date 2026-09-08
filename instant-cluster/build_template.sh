#!/bin/bash
# TEMPLATE BUILD WRAPPER — the only supported way to build a template cluster.
# Exists because building without the ssh key ONCE cost: unclaimable template, thaw + nsenter
# user-repair + double re-freeze, and a template generation whose day-2 machines have no login
# user (qa-nrm1, 2026-08-05). The flag is not optional here.
#   build_template.sh <cluster-name> <vip> <lb-ip> [extra nkp flags...]
set -euo pipefail
NAME="$1"; VIP="$2"; LB="$3"; shift 3
KEY="${NKP_SSH_PUBKEY:-$HOME/.ssh/nkp_cluster.pub}"
[ -f "$KEY" ] || { echo "FATAL: ssh public key $KEY missing — a template built without it is a brick"; exit 1; }
: "${NUTANIX_USER:?set NUTANIX_USER}"; : "${NUTANIX_PASSWORD:?set NUTANIX_PASSWORD}"
IMG="${NKP_NODE_IMAGE:-nkp-rocky-9.7-release-cis-1.35.2-20260701211849.qcow2}"
exec "${NKP_BIN:-$HOME/Documents/nkp/nkp}" create cluster nutanix \
  --cluster-name "$NAME" \
  --endpoint "${NKP_PC_URL:-https://<your-prism-central>:9440}" --insecure \
  --control-plane-endpoint-ip "$VIP" \
  --control-plane-replicas "${NKP_CP_REPLICAS:-1}" --worker-replicas "${NKP_WORKER_REPLICAS:-3}" \
  --control-plane-prism-element-cluster <prism-element-cluster> --worker-prism-element-cluster <prism-element-cluster> \
  --control-plane-subnets <subnet> --worker-subnets <subnet> \
  --control-plane-vm-image "$IMG" --worker-vm-image "$IMG" \
  --control-plane-vcpus 16 --control-plane-memory 32 --control-plane-disk-size 80 \
  --worker-vcpus 16 --worker-memory 32 --worker-disk-size 80 \
  --csi-storage-container SelfServiceContainer \
  --kubernetes-service-load-balancer-ip-range "$LB-$LB" \
  --ssh-username konvoy --ssh-public-key-file "$KEY" \
  --self-managed "$@"
