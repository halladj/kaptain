#!/usr/bin/env bash
set -euo pipefail

swapoff -a
sed -i '/\sswap\s/d' /etc/fstab

# Its name differs between amd64 and arm64.
iface=$(ip -o -4 addr show | awk -v ip="$NODE_IP/" 'index($4, ip) == 1 {print $2}')

curl -sfL https://get.k3s.io |
  INSTALL_K3S_VERSION=$K3S_VERSION INSTALL_K3S_SKIP_START=true \
  K3S_URL="https://$MASTER_IP:6443" K3S_TOKEN=$K3S_TOKEN \
  sh -s - agent --node-name="$NODE_NAME" --node-ip="$NODE_IP" --flannel-iface="$iface"
# Without waiting: the master comes up after the workers.
systemctl start --no-block k3s-agent
