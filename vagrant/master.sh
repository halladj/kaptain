#!/usr/bin/env bash
set -euo pipefail

if [[ $(uname -m) == x86_64 ]] && ! grep -qw avx /proc/cpuinfo; then
  echo 'This VM has no AVX, which stress-ng needs. On Windows: disable Hyper-V (see the README).' >&2
  exit 1
fi

if systemctl is-active --quiet kaptain-campaign; then
  echo 'A campaign is running: stop it first (systemctl stop kaptain-campaign).' >&2
  exit 1
fi

swapoff -a
sed -i '/\sswap\s/d' /etc/fstab

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq docker.io docker-buildx git python3 >/dev/null
curl -LsSf https://astral.sh/uv/0.12.23/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh

# Its name differs between amd64 and arm64.
iface=$(ip -o -4 addr show | awk -v ip="$NODE_IP/" 'index($4, ip) == 1 {print $2}')

curl -sfL https://get.k3s.io |
  INSTALL_K3S_VERSION=$K3S_VERSION K3S_TOKEN=$K3S_TOKEN \
  sh -s - server --node-name="$NODE_NAME" --node-ip="$NODE_IP" --advertise-address="$NODE_IP" \
    --tls-san="$NODE_IP" --flannel-iface="$iface" --flannel-backend=vxlan \
    --disable=traefik --disable=servicelb --secrets-encryption \
    --node-taint=node-role.kubernetes.io/control-plane=true:NoSchedule

echo "Waiting for the $NODE_COUNT nodes..."
ready=0
for _ in $(seq 1 120); do
  ready=$(k3s kubectl get nodes --no-headers 2>/dev/null | awk '$2 == "Ready"' | wc -l)
  (( ready == NODE_COUNT )) && break
  sleep 5
done
if (( ready != NODE_COUNT )); then
  k3s kubectl get nodes -o wide >&2 || true
  echo "Only $ready of $NODE_COUNT nodes are Ready after 10 minutes: did 'vagrant up' start them all?" >&2
  exit 1
fi
k3s kubectl label nodes --all kaptain.io/experiment-node-
for node in $EXPERIMENT_NODES; do
  k3s kubectl label node "$node" kaptain.io/experiment-node=true --overwrite
done

# A clone, not the shared folder: exactly one commit, whatever the host does to line endings.
if [[ ! -e /vagrant/.git ]]; then
  echo 'No .git in the shared folder: get the code with git clone, not as a zip.' >&2
  exit 1
fi
git config --global --add safe.directory /vagrant/.git
git config --global --add safe.directory /vagrant
rm -rf /opt/kaptain
git clone -q /vagrant /opt/kaptain
cd /opt/kaptain
commit=$(git rev-parse HEAD)
echo "Code: $commit"

# Tagged with its commit: the run's report flags the scheduler otherwise.
docker build -q -t "kaptain-scheduler:$commit" plugin/
docker build -q -t kaptain-decider:dev bridge/
docker save "kaptain-scheduler:$commit" kaptain-decider:dev | k3s ctr images import -

k3s kubectl apply -f k8s/decider-rbac.yaml -f k8s/decider.yaml
k3s kubectl -n kube-system rollout status deployment/kaptain-decider --timeout=300s
k3s kubectl apply -f k8s/scheduler-plugin-config.yaml -f k8s/scheduler-plugin.yaml
k3s kubectl -n kube-system set image deployment/kaptain-scheduler "kube-scheduler=kaptain-scheduler:$commit"
k3s kubectl -n kube-system rollout status deployment/kaptain-scheduler --timeout=300s
k3s kubectl get nodes -o wide
