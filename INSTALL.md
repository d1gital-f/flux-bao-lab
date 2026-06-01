# flux-bao-lab — End-to-End Runbook

A reproducible GitOps lab on macOS: a 3-node k3s cluster running on Multipass VMs, bootstrapped with the Flux Operator and driven entirely from this Git repository. It deploys MetalLB (LoadBalancer), OpenBao (HA secrets store), podinfo (demo app), and the Flux Web UI — all reachable directly from the Mac.

> Assumes a clean Mac with nothing installed. Tested on Apple Silicon with Multipass (Virtualization.framework backend).

---

## Architecture

| Component | Role |
|---|---|
| Multipass | Runs three Ubuntu 24.04 VMs whose IPs are directly routable from the Mac |
| k3s | Lightweight Kubernetes: 1 server (control-plane, tainted) + 2 agents (workers) |
| Flux Operator | Installed once via Helm; reconciles a `FluxInstance` that syncs this repo |
| MetalLB | L2-mode LoadBalancer; assigns IPs from the Multipass subnet |
| OpenBao | HA (Raft, 2 replicas) secrets manager with a LoadBalancer UI |
| podinfo | Demo workload, 2 replicas spread across workers behind a LoadBalancer |
| Flux Web UI | Status dashboard, exposed via LoadBalancer + a scoped NetworkPolicy |

Reconciliation order is enforced by Flux `Kustomization` dependencies:

```
flux-system (root) -> infra-controllers -> infra-configs -> apps
```

---

## Repository layout

```
clusters/flux-bao-test/
  flux-instance.yaml        # self-managed FluxInstance (points Flux at this repo)
  infra-controllers.yaml    # Kustomization -> ./infrastructure/controllers
  infra-configs.yaml        # Kustomization -> ./infrastructure/configs (dependsOn controllers)
  apps.yaml                 # Kustomization -> ./apps (dependsOn configs)
infrastructure/controllers/
  metallb.yaml              # MetalLB HelmRepository + HelmRelease (FRR disabled)
  openbao.yaml              # OpenBao HA HelmRepository + HelmRelease
infrastructure/configs/
  metallb-config.yaml       # IPAddressPool + L2Advertisement
  flux-ui-service.yaml      # LoadBalancer Service for the Flux UI
  flux-web-netpol.yaml      # NetworkPolicy allowing external access to the UI
apps/
  podinfo.yaml              # podinfo HelmRepository + HelmRelease + PodDisruptionBudget
docs/
  RUNBOOK.md                # this file
```

The full manifest contents are in [Appendix A](#appendix-a--manifests).

---

## Prerequisites — install tooling on a fresh Mac

```zsh
# 1. Homebrew
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
eval "$(/opt/homebrew/bin/brew shellenv)"   # Apple Silicon; Intel: /usr/local/bin/brew

# 2. Tools
brew install multipass kubernetes-cli helm jq git fluxcd/tap/flux
```

You also need a GitHub repo. This runbook assumes `https://github.com/d1gital-f/flux-bao-lab`.

> **Multipass note:** if you are not a local admin, `multipass exec` may fail with
> `socket access denied`. An admin must run `sudo multipass set local.passphrase`
> and share it; you then run `multipass authenticate` once.

---

## Step 1 — Clone the repo

```zsh
cd ~
git clone https://github.com/d1gital-f/flux-bao-lab.git
cd flux-bao-lab
```

If starting from an empty repo, create the manifests from [Appendix A](#appendix-a--manifests) and push them before continuing.

---

## Step 2 — Provision the k3s cluster

```zsh
# Launch three VMs
multipass launch 24.04 --name k3s-cp --cpus 2 --memory 4G --disk 20G
multipass launch 24.04 --name k3s-w1 --cpus 2 --memory 4G --disk 20G
multipass launch 24.04 --name k3s-w2 --cpus 2 --memory 4G --disk 20G

# Server config MUST be in place before installing k3s, so the flags actually apply.
# - disable servicelb (klipper) so it doesn't fight MetalLB
# - taint the control-plane so workloads only land on workers
multipass exec k3s-cp -- sudo mkdir -p /etc/rancher/k3s
multipass exec k3s-cp -- sudo bash -c 'cat > /etc/rancher/k3s/config.yaml <<EOF
disable:
  - servicelb
node-taint:
  - "node-role.kubernetes.io/control-plane=:NoSchedule"
EOF'

# Install the k3s server
multipass exec k3s-cp -- bash -c "curl -sfL https://get.k3s.io | sh -"

# Capture server IP + join token
VM_IP=$(multipass info k3s-cp --format json | jq -r '.info["k3s-cp"].ipv4[0]')
TOKEN=$(multipass exec k3s-cp -- sudo cat /var/lib/rancher/k3s/server/node-token)
echo "VM_IP=$VM_IP"

# Join the workers
multipass exec k3s-w1 -- bash -c "curl -sfL https://get.k3s.io | K3S_URL=https://${VM_IP}:6443 K3S_TOKEN=${TOKEN} sh -"
multipass exec k3s-w2 -- bash -c "curl -sfL https://get.k3s.io | K3S_URL=https://${VM_IP}:6443 K3S_TOKEN=${TOKEN} sh -"

# Pull kubeconfig to the Mac, rewriting the server address
mkdir -p ~/.kube
multipass exec k3s-cp -- sudo cat /etc/rancher/k3s/k3s.yaml | sed "s/127.0.0.1/$VM_IP/" > ~/.kube/config
chmod 600 ~/.kube/config
```

**Verify before continuing:**

```zsh
kubectl get nodes                                  # 3 nodes Ready
kubectl describe nodes | grep -i taint             # taint only on k3s-cp
kubectl get pods -n kube-system | grep svclb       # must return NOTHING
```

---

## Step 3 — Align the MetalLB pool with your subnet

Multipass assigns VM IPs from a `/24` that varies per machine. Check it and make sure the MetalLB pool (and the Flux UI NetworkPolicy `ipBlock`) live in the same subnet.

```zsh
multipass list | grep k3s
grep -A2 addresses infrastructure/configs/metallb-config.yaml
```

If your subnet differs from `192.168.252.x`, update **both** files and push:

```zsh
# Example: replace 192.168.252 with your actual subnet prefix
NEW=192.168.64   # <-- set to your subnet
sed -i '' "s/192\.168\.252/${NEW}/g" infrastructure/configs/metallb-config.yaml
sed -i '' "s/192\.168\.252/${NEW}/g" infrastructure/configs/flux-web-netpol.yaml

git add infrastructure/configs/
git commit -m "metallb: align pool and netpol with subnet ${NEW}.0/24"
git push
```

Pick a pool range high in the subnet (e.g. `.200-.250`) so it never collides with VM IPs.

---

## Step 4 — Bootstrap Flux

The Flux Operator is the only thing installed imperatively; everything else flows from Git.

```zsh
helm install flux-operator \
  oci://ghcr.io/controlplaneio-fluxcd/charts/flux-operator \
  --namespace flux-system --create-namespace \
  --set web.enabled=true \
  --set web.networkPolicy.create=false \
  --wait

# Hand Flux the repo; the operator creates the GitRepository + root Kustomization
kubectl apply -f clusters/flux-bao-test/flux-instance.yaml
```

Watch the cascade:

```zsh
flux get kustomizations --watch
```

If the dependency chain lags (a known timing quirk), nudge it:

```zsh
flux reconcile kustomization infra-configs
flux reconcile kustomization apps
```

Confirm:

```zsh
kubectl get pods -A -o wide
kubectl get svc -A | grep LoadBalancer
```

Expect OpenBao and podinfo pods only on the two workers, and LoadBalancer IPs assigned for `traefik`, `flux-operator-ui`, `podinfo`, and `openbao-ui`.

---

## Step 5 — Initialize and unseal OpenBao

OpenBao starts sealed (pods show `0/1`). With `OrderedReady`, `openbao-1` only appears after `openbao-0` is unsealed and Ready.

```zsh
# Initialize on pod 0
kubectl -n openbao exec openbao-0 -- \
  bao operator init -key-shares=5 -key-threshold=3 -format=json > openbao-init.json

# Extract three unseal keys
K1=$(jq -r '.unseal_keys_b64[0]' openbao-init.json)
K2=$(jq -r '.unseal_keys_b64[1]' openbao-init.json)
K3=$(jq -r '.unseal_keys_b64[2]' openbao-init.json)

# Unseal pod 0 (becomes leader)
for K in "$K1" "$K2" "$K3"; do kubectl -n openbao exec openbao-0 -- bao operator unseal "$K"; done

# Wait for openbao-1 to appear, then unseal it
kubectl -n openbao get pods -w   # Ctrl-C once openbao-1 is Running 0/1
for K in "$K1" "$K2" "$K3"; do kubectl -n openbao exec openbao-1 -- bao operator unseal "$K"; done

# Verify HA
kubectl -n openbao exec openbao-0 -- bao status
kubectl -n openbao exec openbao-0 -- \
  env BAO_TOKEN=$(jq -r '.root_token' openbao-init.json) bao operator raft list-peers
```

> **Keep `openbao-init.json` safe and never commit it** (it's git-ignored). OpenBao
> re-seals on every pod restart with the file backend, so you will need these keys again.

---

## Step 6 — Access everything from the Mac

Because Multipass VM IPs are routable from the host, the MetalLB IPs work directly — no port-forwards, tunnels, or bridges.

```zsh
FLUX_IP=$(kubectl -n flux-system get svc flux-operator-ui -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
PODINFO_IP=$(kubectl -n podinfo get svc podinfo -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
BAO_IP=$(kubectl -n openbao get svc openbao-ui -o jsonpath='{.status.loadBalancer.ingress[0].ip}')

echo "Flux UI:    http://${FLUX_IP}:9080"
echo "podinfo:    http://${PODINFO_IP}:9898"
echo "OpenBao UI: http://${BAO_IP}:8200/ui"
echo "Root token: $(jq -r '.root_token' openbao-init.json)"
```

Verify load balancing across both podinfo replicas:

```zsh
for i in {1..10}; do curl -s http://${PODINFO_IP}:9898 | jq -r .hostname; done | sort | uniq -c
```

You should see both pod hostnames in the output.

---

## Daily workflow

From here on, the cluster is driven by Git. To change anything, edit a manifest and push:

```zsh
git add . && git commit -m "..." && git push
flux reconcile source git flux-system   # optional: pull immediately
```

---

## Teardown

```zsh
multipass delete --purge k3s-cp k3s-w1 k3s-w2
rm -f openbao-init.json ~/.kube/config
```

Re-running Steps 2–5 rebuilds the whole environment from the same repo.

---

## Troubleshooting — lessons learned

**`svclb-*` pods exist / MetalLB IPs unreachable.** k3s's built-in klipper LoadBalancer (`servicelb`) was not disabled and is fighting MetalLB. The `disable: [servicelb]` must be in `/etc/rancher/k3s/config.yaml` *before* k3s is installed; setting it after requires a reinstall.

**Workloads landing on the control-plane.** The CP taint wasn't applied at install time. Either fix the k3s config and reinstall, or `kubectl taint node k3s-cp node-role.kubernetes.io/control-plane=:NoSchedule --overwrite`.

**OpenBao pod stuck `Pending` after rescheduling.** k3s's local-path provisioner pins each PV to the node it was created on. If a pod must move nodes, delete its PVC (`kubectl -n openbao delete pvc data-openbao-0`) so a fresh volume provisions on the new node.

**OpenBao HelmRelease never becomes Ready.** OpenBao's readiness probe fails until unsealed, by design. `install.disableWait: true` / `upgrade.disableWait: true` in the HelmRelease let Helm complete without waiting on pod readiness.

**OpenBao config change doesn't take effect.** The StatefulSet uses `updateStrategyType: OnDelete`, so changes don't roll automatically — `kubectl -n openbao delete pod openbao-N` to apply.

**Flux UI returns `Connection refused`.** The chart's web server needs `web.enabled=true` (it serves on port 9080). Confirm the operator container exposes the `http-web` port and logs `starting web server ... port 9080`.

**Flux UI refuses externally but works inside the cluster.** A NetworkPolicy is blocking it. External LoadBalancer traffic is SNAT'd to a node/cluster IP, which doesn't match a `namespaceSelector`. The `flux-web-netpol.yaml` adds an `ipBlock` for the node subnet and pod CIDR while keeping default-deny intact.

**MetalLB pods CrashLooping (`frr-k8s`).** MetalLB chart 0.16+ defaults to FRR-K8s (BGP) mode. For plain L2 on kind/k3s, set `speaker.frr.enabled: false` and `frrk8s.enabled: false`.

**`docker-mac-net-connect` doesn't work.** It targets Docker Desktop's VM networking and does not work with Colima or Multipass. Multipass routes VM IPs to the host natively, so it isn't needed here.

**zsh: `command not found: #` when pasting heredocs.** zsh doesn't honor `#` comments interactively by default. Run `setopt interactive_comments` or save the block to a file and run it.

---

## Appendix A — manifests

<details>
<summary><code>clusters/flux-bao-test/flux-instance.yaml</code></summary>

```yaml
apiVersion: fluxcd.controlplane.io/v1
kind: FluxInstance
metadata:
  name: flux
  namespace: flux-system
  annotations:
    fluxcd.controlplane.io/reconcileEvery: "1h"
    fluxcd.controlplane.io/reconcileTimeout: "5m"
spec:
  distribution:
    version: "2.x"
    registry: "ghcr.io/fluxcd"
  components:
    - source-controller
    - kustomize-controller
    - helm-controller
    - notification-controller
  cluster:
    type: kubernetes
    multitenant: false
    networkPolicy: true
    domain: "cluster.local"
  sync:
    kind: GitRepository
    url: "https://github.com/d1gital-f/flux-bao-lab.git"
    ref: "refs/heads/main"
    path: "clusters/flux-bao-test"
```
</details>

<details>
<summary><code>clusters/flux-bao-test/infra-controllers.yaml</code></summary>

```yaml
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: infra-controllers
  namespace: flux-system
spec:
  interval: 10m
  retryInterval: 1m
  timeout: 5m
  prune: true
  wait: true
  sourceRef:
    kind: GitRepository
    name: flux-system
  path: ./infrastructure/controllers
```
</details>

<details>
<summary><code>clusters/flux-bao-test/infra-configs.yaml</code></summary>

```yaml
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: infra-configs
  namespace: flux-system
spec:
  dependsOn:
    - name: infra-controllers
  interval: 10m
  retryInterval: 1m
  timeout: 5m
  prune: true
  sourceRef:
    kind: GitRepository
    name: flux-system
  path: ./infrastructure/configs
```
</details>

<details>
<summary><code>clusters/flux-bao-test/apps.yaml</code></summary>

```yaml
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: apps
  namespace: flux-system
spec:
  dependsOn:
    - name: infra-configs
  interval: 10m
  retryInterval: 1m
  timeout: 5m
  prune: true
  sourceRef:
    kind: GitRepository
    name: flux-system
  path: ./apps
```
</details>

<details>
<summary><code>infrastructure/controllers/metallb.yaml</code></summary>

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: metallb-system
---
apiVersion: source.toolkit.fluxcd.io/v1
kind: HelmRepository
metadata:
  name: metallb
  namespace: metallb-system
spec:
  interval: 1h
  url: https://metallb.github.io/metallb
---
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: metallb
  namespace: metallb-system
spec:
  interval: 10m
  chart:
    spec:
      chart: metallb
      version: '0.16.1'
      sourceRef:
        kind: HelmRepository
        name: metallb
      interval: 5m
  install:
    crds: Create
  upgrade:
    crds: CreateReplace
  values:
    speaker:
      frr:
        enabled: false
    frrk8s:
      enabled: false
```
</details>

<details>
<summary><code>infrastructure/controllers/openbao.yaml</code></summary>

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: openbao
---
apiVersion: source.toolkit.fluxcd.io/v1
kind: HelmRepository
metadata:
  name: openbao
  namespace: openbao
spec:
  interval: 1h
  url: https://openbao.github.io/openbao-helm
---
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: openbao
  namespace: openbao
spec:
  interval: 10m
  chart:
    spec:
      chart: openbao
      sourceRef:
        kind: HelmRepository
        name: openbao
      interval: 5m
  install:
    disableWait: true
  upgrade:
    disableWait: true
  values:
    server:
      standalone:
        enabled: false
      ha:
        enabled: true
        replicas: 2
        raft:
          enabled: true
          setNodeId: true
          config: |
            ui = true
            listener "tcp" {
              tls_disable = 1
              address = "[::]:8200"
              cluster_address = "[::]:8201"
            }
            storage "raft" {
              path = "/openbao/data"
              retry_join {
                leader_api_addr = "http://openbao-0.openbao-internal:8200"
              }
              retry_join {
                leader_api_addr = "http://openbao-1.openbao-internal:8200"
              }
            }
            service_registration "kubernetes" {}
      dataStorage:
        enabled: true
        size: 1Gi
        accessMode: ReadWriteOnce
      updateStrategyType: "OnDelete"
    ui:
      enabled: true
      serviceType: LoadBalancer
```
</details>

<details>
<summary><code>infrastructure/configs/metallb-config.yaml</code></summary>

```yaml
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata:
  name: kind-pool
  namespace: metallb-system
spec:
  addresses:
    - 192.168.252.200-192.168.252.250
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata:
  name: kind-l2
  namespace: metallb-system
spec:
  ipAddressPools:
    - kind-pool
```
</details>

<details>
<summary><code>infrastructure/configs/flux-ui-service.yaml</code></summary>

```yaml
apiVersion: v1
kind: Service
metadata:
  name: flux-operator-ui
  namespace: flux-system
spec:
  type: LoadBalancer
  selector:
    app.kubernetes.io/name: flux-operator
    app.kubernetes.io/instance: flux-operator
  ports:
    - name: http-web
      port: 9080
      targetPort: 9080
```
</details>

<details>
<summary><code>infrastructure/configs/flux-web-netpol.yaml</code></summary>

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: flux-web-access
  namespace: flux-system
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: flux-operator
      app.kubernetes.io/instance: flux-operator
  policyTypes:
    - Ingress
  ingress:
    - from:
        - namespaceSelector: {}
        - ipBlock:
            cidr: 192.168.252.0/24
        - ipBlock:
            cidr: 10.42.0.0/16
      ports:
        - port: 9080
          protocol: TCP
```
</details>

<details>
<summary><code>apps/podinfo.yaml</code></summary>

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: podinfo
---
apiVersion: source.toolkit.fluxcd.io/v1
kind: HelmRepository
metadata:
  name: podinfo
  namespace: podinfo
spec:
  interval: 10m
  url: https://stefanprodan.github.io/podinfo
---
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: podinfo
  namespace: podinfo
spec:
  interval: 10m
  chart:
    spec:
      chart: podinfo
      version: '6.x'
      sourceRef:
        kind: HelmRepository
        name: podinfo
      interval: 5m
  values:
    replicaCount: 2
    service:
      type: LoadBalancer
      port: 9898
    affinity:
      podAntiAffinity:
        requiredDuringSchedulingIgnoredDuringExecution:
          - labelSelector:
              matchExpressions:
                - key: app.kubernetes.io/name
                  operator: In
                  values:
                    - podinfo
            topologyKey: kubernetes.io/hostname
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: podinfo
  namespace: podinfo
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: podinfo
```
</details>

<details>
<summary><code>.gitignore</code></summary>

```
openbao-init.json
```
</details>
