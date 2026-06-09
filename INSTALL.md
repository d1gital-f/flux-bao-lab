# flux-bao-lab — Full Install and Troubleshooting Guide

A reproducible GitOps lab on macOS using Multipass, k3s, Flux Operator, MetalLB, Traefik, OpenBao, podinfo, Flux MCP, kagent, Ollama, and Claude Desktop MCP.

This document is a full rebuild/runbook for the current lab state. It includes the work added during the kagent/Ollama/Claude Desktop session:

- kagent installed by Flux through Helm.
- kagent UI and MCP exposed through Traefik hostnames.
- kagent UI/controller also optionally exposed by direct MetalLB `LoadBalancer` Services.
- Claude Desktop connected to kagent through local MCP using `mcp-proxy`.
- Ollama deployed in-cluster for kagent agents.
- A read-only `lab-k8s-reader` kagent agent backed by Ollama.
- Troubleshooting notes for the failures encountered.

> Important: never commit `openbao-init.json`, unseal keys, root tokens, or Claude/API credentials. If `openbao-init.json` appears in a zip export, treat that zip as sensitive.

---

## Architecture

| Component | Role |
|---|---|
| Multipass | Three Ubuntu 24.04 VMs directly routable from the Mac |
| k3s | One tainted control-plane VM and two worker VMs |
| Flux Operator | Bootstraps Flux and reconciles this Git repo |
| MetalLB | L2 LoadBalancer implementation for Multipass subnet IPs |
| Traefik | k3s bundled ingress controller, pinned to one MetalLB IP |
| OpenBao | HA Raft secret store, manually initialized/unsealed |
| podinfo | Demo application |
| Flux UI | Flux Operator web UI |
| Flux MCP | MCP server for Flux operations |
| kagent | Kubernetes-native AI agent platform |
| Ollama | In-cluster local LLM backend for kagent |
| Claude Desktop | Local MCP client connected to kagent through `mcp-proxy` |

Access paths:

```text
Direct MetalLB:
  service-name.namespace.direct.fmgb.lab -> service LoadBalancer IP + port

Traefik hostname access:
  app.prod1.fmgb.lab -> Traefik 192.168.252.249 -> backend Service
```

The working Claude/kagent chain is:

```text
Claude Desktop
  -> local MCP server command: uvx mcp-proxy
  -> http://kagent-mcp.prod1.fmgb.lab/mcp
  -> Traefik
  -> kagent-controller
  -> lab-k8s-reader agent
  -> Ollama llama3.2
  -> kagent-tools MCP
  -> Kubernetes API
```

---

## Current repository layout

```text
clusters/flux-bao-test/
  flux-instance.yaml        # FluxInstance that syncs this Git repo
  infra-controllers.yaml    # Flux Kustomization -> ./infrastructure/controllers
  infra-configs.yaml        # Flux Kustomization -> ./infrastructure/configs
  apps.yaml                 # Flux Kustomization -> ./apps

infrastructure/controllers/
  metallb.yaml              # MetalLB HelmRepository + HelmRelease
  openbao.yaml              # OpenBao HelmRepository + HelmRelease
  flux-mcp.yaml             # Flux MCP ResourceSet
  kagent.yaml               # kagent CRDs + kagent HelmRelease
  ollama.yaml               # Ollama Deployment + PVC + Service

infrastructure/configs/
  metallb-config.yaml       # MetalLB IPAddressPool + L2Advertisement
  traefik-pin.yaml          # Pins Traefik LoadBalancer IP
  flux-ui-service.yaml      # Direct MetalLB Service for Flux UI
  flux-web-netpol.yaml      # NetworkPolicy for Flux UI
  flux-mcp-service.yaml     # Direct MetalLB Service for Flux MCP
  flux-mcp-netpol.yaml      # NetworkPolicy for Flux MCP
  ingress-flux.yaml         # Traefik Ingress for Flux UI + Flux MCP
  ingress-openbao.yaml      # Traefik Ingress for OpenBao UI
  ingress-kagent.yaml       # Traefik Ingress for kagent UI + kagent MCP

apps/
  podinfo.yaml              # podinfo HelmRepository + HelmRelease + PDB
  podinfo-ingress.yaml      # Traefik Ingress for podinfo
  kagent-test-agent.yaml    # lab-k8s-reader kagent Agent

scripts/
  metallb-hosts.zsh         # Prints copy/paste /etc/hosts block
```

Current Flux reconciliation order:

```text
flux-system root -> infra-controllers -> infra-configs -> apps
```

---

## Prerequisites on macOS

```zsh
# Homebrew
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
eval "$(/opt/homebrew/bin/brew shellenv)"   # Apple Silicon; Intel: /usr/local/bin/brew

# Core tools
brew install multipass kubernetes-cli helm jq git fluxcd/tap/flux

# Needed for Claude Desktop MCP bridge
brew install uv

# Optional editors
brew install --cask visual-studio-code
# or use VSCodium if you prefer:
# brew install --cask vscodium
```

Multipass note: if `multipass exec` fails with `socket access denied`, an admin must run:

```zsh
sudo multipass set local.passphrase
```

Then the non-admin user runs:

```zsh
multipass authenticate
```

---

## Step 1 — Clone the repo

```zsh
cd ~
git clone https://github.com/d1gital-f/flux-bao-lab.git
cd flux-bao-lab
```

---

## Step 2 — Provision k3s on Multipass

```zsh
multipass launch 24.04 --name k3s-cp --cpus 2 --memory 4G --disk 20G
multipass launch 24.04 --name k3s-w1 --cpus 2 --memory 4G --disk 20G
multipass launch 24.04 --name k3s-w2 --cpus 2 --memory 4G --disk 20G

# Server config must exist before k3s install.
multipass exec k3s-cp -- sudo mkdir -p /etc/rancher/k3s
multipass exec k3s-cp -- sudo bash -c 'cat > /etc/rancher/k3s/config.yaml <<EOF
disable:
  - servicelb
node-taint:
  - "node-role.kubernetes.io/control-plane=:NoSchedule"
EOF'

# Install k3s server
multipass exec k3s-cp -- bash -c "curl -sfL https://get.k3s.io | sh -"

# Join workers
VM_IP=$(multipass info k3s-cp --format json | jq -r '.info["k3s-cp"].ipv4[0]')
TOKEN=$(multipass exec k3s-cp -- sudo cat /var/lib/rancher/k3s/server/node-token)
echo "VM_IP=$VM_IP"

multipass exec k3s-w1 -- bash -c "curl -sfL https://get.k3s.io | K3S_URL=https://${VM_IP}:6443 K3S_TOKEN=${TOKEN} sh -"
multipass exec k3s-w2 -- bash -c "curl -sfL https://get.k3s.io | K3S_URL=https://${VM_IP}:6443 K3S_TOKEN=${TOKEN} sh -"

# Pull kubeconfig to the Mac
mkdir -p ~/.kube
multipass exec k3s-cp -- sudo cat /etc/rancher/k3s/k3s.yaml | sed "s/127.0.0.1/$VM_IP/" > ~/.kube/config
chmod 600 ~/.kube/config
```

Verify:

```zsh
kubectl get nodes -o wide
kubectl describe nodes | grep -i taint
kubectl get pods -n kube-system | grep svclb     # should return nothing
```

Expected:

```text
k3s-cp   Ready   control-plane
k3s-w1   Ready   <none>
k3s-w2   Ready   <none>
```

---

## Step 3 — Align MetalLB and NetworkPolicy subnet

Multipass usually allocates a private `/24` subnet. This lab currently uses `192.168.252.0/24` and MetalLB range `192.168.252.200-192.168.252.250`.

Check your VM IPs:

```zsh
multipass list | grep k3s
```

Check repo references:

```zsh
grep -R '192\.168\.252' infrastructure scripts clusters apps 2>/dev/null
```

If your subnet differs, retarget before bootstrapping Flux:

```zsh
NEW=192.168.64   # example: change this to your Multipass /24 prefix

grep -rl '192\.168\.252' infrastructure scripts clusters apps 2>/dev/null \
  | xargs sed -i '' "s/192\.168\.252/${NEW}/g"

git add infrastructure scripts clusters apps
git commit -m "retarget lab subnet to ${NEW}.0/24"
git push
```

Keep the MetalLB pool high in the subnet so it does not collide with VM IPs.

---

## Step 4 — Bootstrap Flux Operator

The Flux Operator Helm install is the only imperative install. Everything else should flow from Git.

```zsh
helm install flux-operator \
  oci://ghcr.io/controlplaneio-fluxcd/charts/flux-operator \
  --namespace flux-system --create-namespace \
  --set web.enabled=true \
  --set web.networkPolicy.create=false \
  --wait

kubectl apply -f clusters/flux-bao-test/flux-instance.yaml
```

Watch reconciliation:

```zsh
flux get kustomizations --watch
```

Nudge if needed:

```zsh
flux reconcile source git flux-system
flux reconcile kustomization infra-controllers
flux reconcile kustomization infra-configs
flux reconcile kustomization apps
```

Verify:

```zsh
flux get all -A
kubectl get pods -A -o wide
kubectl get svc -A | grep LoadBalancer
kubectl get ingress -A
```

---

## Step 5 — Initialize and unseal OpenBao

OpenBao starts sealed. With two replicas and ordered startup, unseal `openbao-0`, then wait for `openbao-1`, then unseal it.

```zsh
kubectl -n openbao exec openbao-0 -- \
  bao operator init -key-shares=5 -key-threshold=3 -format=json > openbao-init.new.json \
  && mv openbao-init.new.json openbao-init.json

jq . openbao-init.json

K1=$(jq -r '.unseal_keys_b64[0]' openbao-init.json)
K2=$(jq -r '.unseal_keys_b64[1]' openbao-init.json)
K3=$(jq -r '.unseal_keys_b64[2]' openbao-init.json)

for K in "$K1" "$K2" "$K3"; do
  kubectl -n openbao exec openbao-0 -- bao operator unseal "$K"
done

kubectl -n openbao get pods -w     # Ctrl-C when openbao-1 appears/runs

for K in "$K1" "$K2" "$K3"; do
  kubectl -n openbao exec openbao-1 -- bao operator unseal "$K"
done

cp openbao-init.json ~/openbao-init.backup.json
kubectl -n openbao exec openbao-0 -- bao status
kubectl -n openbao exec openbao-0 -- \
  env BAO_TOKEN=$(jq -r '.root_token' openbao-init.json) bao operator raft list-peers
```

Secure the key file:

```zsh
cat >> .gitignore <<'EOF'
openbao-init.json
openbao-init.*.json
*.unseal
EOF

git rm --cached openbao-init.json 2>/dev/null || true
git add .gitignore
git commit -m "gitignore OpenBao secrets"
git push

git check-ignore openbao-init.json && echo "IGNORED ok"
git ls-files | grep -i 'openbao-init\|unseal' || true
```

If `git ls-files` prints a secret file, it is still tracked and must be removed from Git.

---

## Step 6 — Access paths and hostnames

### 6.1 Direct MetalLB IP discovery

```zsh
kubectl get svc -A -o json | jq -r '
  .items[]
  | select(.spec.type == "LoadBalancer")
  | [
      .metadata.namespace,
      .metadata.name,
      (.status.loadBalancer.ingress[0].ip // "<pending>"),
      ([.spec.ports[] | "\(.port)->\(.targetPort // "-")"] | join(","))
    ]
  | @tsv
' | awk 'BEGIN { printf "%-16s %-32s %-18s %s\n", "NAMESPACE", "SERVICE", "EXTERNAL-IP", "PORTS" }
         { printf "%-16s %-32s %-18s %s\n", $1, $2, $3, $4 }'
```

Expected shape:

```text
flux-system      flux-operator-ui       192.168.252.200    9080->9080
openbao          openbao-ui             192.168.252.201    8200->8200
kagent           kagent-controller      192.168.252.202    8083->8083
podinfo          podinfo                192.168.252.203    9898->http,9999->grpc
flux-system      flux-operator-mcp-lb   192.168.252.204    9090->9090
kagent           kagent-ui              192.168.252.205    8080->8080
kube-system      traefik                192.168.252.249    80->web,443->websecure
```

### 6.2 `/etc/hosts` generator

The script should print only copy/paste host lines:

```zsh
./scripts/metallb-hosts.zsh
```

Expected output shape:

```text
# flux-bao-lab hosts start
192.168.252.249  fluxmcp.prod1.fmgb.lab fluxui.prod1.fmgb.lab kagent-mcp.prod1.fmgb.lab kagent.prod1.fmgb.lab openbao.prod1.fmgb.lab podinfo.prod1.fmgb.lab
192.168.252.200  flux-operator-ui.flux-system.direct.fmgb.lab
192.168.252.201  openbao-ui.openbao.direct.fmgb.lab
192.168.252.202  kagent-controller.kagent.direct.fmgb.lab
192.168.252.203  podinfo.podinfo.direct.fmgb.lab
192.168.252.204  flux-operator-mcp-lb.flux-system.direct.fmgb.lab
192.168.252.205  kagent-ui.kagent.direct.fmgb.lab
192.168.252.249  traefik.kube-system.direct.fmgb.lab
# flux-bao-lab hosts end
```

To apply safely:

```zsh
sudo sed -i '' '/# flux-bao-lab hosts start/,/# flux-bao-lab hosts end/d' /etc/hosts
./scripts/metallb-hosts.zsh | sudo tee -a /etc/hosts >/dev/null

grep -A20 'flux-bao-lab hosts start' /etc/hosts
```

### 6.3 Real DNS wildcard for Traefik

`/etc/hosts` cannot do wildcards. If using DNS, create one wildcard A record:

```dns
*.prod1.fmgb.lab. 300 IN A 192.168.252.249
```

The direct service hostnames still need either explicit DNS records or `/etc/hosts` entries.

### 6.4 Useful URLs

Traefik hostnames:

```text
Flux UI:     http://fluxui.prod1.fmgb.lab
Flux MCP:    http://fluxmcp.prod1.fmgb.lab
OpenBao UI:  http://openbao.prod1.fmgb.lab/ui/
podinfo:     http://podinfo.prod1.fmgb.lab
kagent UI:   http://kagent.prod1.fmgb.lab
kagent MCP:  http://kagent-mcp.prod1.fmgb.lab/mcp
```

Direct MetalLB hostnames, when generated by the script:

```text
Flux UI:     http://flux-operator-ui.flux-system.direct.fmgb.lab:9080
Flux MCP:    http://flux-operator-mcp-lb.flux-system.direct.fmgb.lab:9090/mcp
OpenBao UI:  http://openbao-ui.openbao.direct.fmgb.lab:8200/ui/
podinfo:     http://podinfo.podinfo.direct.fmgb.lab:9898
kagent UI:   http://kagent-ui.kagent.direct.fmgb.lab:8080
kagent MCP:  http://kagent-controller.kagent.direct.fmgb.lab:8083/mcp
```

---

## Step 7 — kagent status

kagent is installed by Flux from `infrastructure/controllers/kagent.yaml` using two Helm releases:

```text
kagent-crds
kagent
```

Verify:

```zsh
flux get helmreleases -n kagent
kubectl -n kagent get pods
kubectl -n kagent get svc
kubectl -n kagent get ingress
kubectl get crd | grep kagent
```

Expected kagent pods:

```text
kagent-controller
kagent-kmcp-controller-manager
kagent-postgresql
kagent-tools
kagent-ui
```

Expected services:

```text
kagent-controller  8083
kagent-ui          8080
kagent-tools       8084
```

Expected ingress:

```text
kagent.prod1.fmgb.lab      -> kagent-ui:8080
kagent-mcp.prod1.fmgb.lab  -> kagent-controller:8083 path /mcp
```

Basic route check:

```zsh
curl -I http://kagent.prod1.fmgb.lab
curl -sv http://kagent-mcp.prod1.fmgb.lab/mcp 2>&1 | head -40
```

A bare `GET /mcp` returning this is good:

```text
Bad Request: GET requires an Mcp-Session-Id header
```

It means the request reached kagent. A normal `curl` is not a full MCP client.

---

## Step 8 — Ollama in-cluster

Ollama is installed from `infrastructure/controllers/ollama.yaml`.

Verify pod, PVC, and service:

```zsh
kubectl -n kagent get pods,pvc,svc | grep ollama
```

If the model was not pulled by the pod lifecycle hook, pull it manually:

```zsh
kubectl -n kagent exec deploy/ollama -- ollama pull llama3.2
```

Verify model availability:

```zsh
kubectl -n kagent exec deploy/ollama -- ollama list

kubectl -n kagent run ollama-check --rm -it \
  --image=curlimages/curl \
  --restart=Never \
  -- curl -s http://ollama.kagent.svc.cluster.local:11434/api/tags
```

Direct generation test:

```zsh
kubectl -n kagent exec deploy/ollama -- \
  ollama run llama3.2 "Reply with only: ok"
```

On the 4 GiB worker VM this is CPU-only and may be slow.

---

## Step 9 — Make kagent use in-cluster Ollama

The working `ModelConfig` must point to the in-cluster Ollama service and must use a modest context size.

Working runtime state:

```yaml
apiVersion: kagent.dev/v1alpha2
kind: ModelConfig
metadata:
  name: default-model-config
  namespace: kagent
spec:
  model: llama3.2
  provider: Ollama
  ollama:
    host: http://ollama.kagent.svc.cluster.local:11434
    options:
      num_ctx: "4096"
```

Manual patch used during troubleshooting:

```zsh
kubectl -n kagent patch modelconfig default-model-config --type merge -p '{
  "spec": {
    "model": "llama3.2",
    "provider": "Ollama",
    "ollama": {
      "host": "http://ollama.kagent.svc.cluster.local:11434",
      "options": {
        "num_ctx": "4096"
      }
    }
  }
}'

kubectl -n kagent rollout restart deploy/lab-k8s-reader
kubectl -n kagent rollout status deploy/lab-k8s-reader
```

For GitOps permanence, prefer putting this into the `kagent` HelmRelease values rather than relying on a manual patch. In `infrastructure/controllers/kagent.yaml`, under `spec.values.providers`, use this structure:

```yaml
providers:
  default: ollama
  ollama:
    provider: Ollama
    model: llama3.2
    config:
      host: http://ollama.kagent.svc.cluster.local:11434
      options:
        num_ctx: "4096"
```

Then commit and reconcile:

```zsh
git add infrastructure/controllers/kagent.yaml
git commit -m "configure kagent model for in-cluster Ollama"
git push

flux reconcile source git flux-system
flux reconcile kustomization infra-controllers
kubectl -n kagent get modelconfig default-model-config -o yaml
```

Why `4096`: `64000` was too large for the 4 GiB CPU-only Multipass worker and caused agent requests to disconnect/timeout. `4096` worked.

If `llama3.2` remains too slow, try a smaller model:

```zsh
kubectl -n kagent exec deploy/ollama -- ollama pull qwen2.5:0.5b

kubectl -n kagent patch modelconfig default-model-config --type merge -p '{
  "spec": {
    "model": "qwen2.5:0.5b",
    "provider": "Ollama",
    "ollama": {
      "host": "http://ollama.kagent.svc.cluster.local:11434",
      "options": {
        "num_ctx": "2048"
      }
    }
  }
}'
```

---

## Step 10 — kagent test agent

The test agent is `apps/kagent-test-agent.yaml`:

```text
Agent: lab-k8s-reader
Namespace: kagent
Type: Declarative
ModelConfig: default-model-config
Tools:
  - k8s_get_available_api_resources
  - k8s_get_resources
```

Verify:

```zsh
kubectl -n kagent get agents
kubectl -n kagent get agent lab-k8s-reader -o jsonpath='{range .status.conditions[*]}{.type}={.status} {.reason}{"\n"}{end}'
```

Expected:

```text
Accepted=True Reconciled
Ready=True DeploymentReady
```

Agent pod:

```zsh
kubectl -n kagent get pods | grep lab-k8s-reader
kubectl -n kagent logs deploy/lab-k8s-reader --tail=100
```

---

## Step 11 — Claude Desktop MCP setup

Claude Desktop uses local MCP servers from:

```text
~/Library/Application Support/Claude/claude_desktop_config.json
```

### 11.1 Back up before editing

```zsh
CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
mkdir -p "$HOME/Library/Application Support/Claude"
cp "$CONFIG" "$CONFIG.bak.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
```

Validate after every edit:

```zsh
jq . "$CONFIG" >/dev/null && echo "JSON OK"
jq '.mcpServers | keys' "$CONFIG"
```

### 11.2 Add kagent MCP server with `mcp-proxy`

Install `uv` if missing:

```zsh
brew install uv
which uvx
```

Safely merge the `kagent-lab` entry without deleting existing MCP servers:

```zsh
CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
cp "$CONFIG" "$CONFIG.pre-kagent.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true

jq '.mcpServers["kagent-lab"] = {
  "command": "uvx",
  "args": [
    "mcp-proxy",
    "http://kagent-mcp.prod1.fmgb.lab/mcp",
    "--transport=streamablehttp"
  ],
  "env": {
    "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
  }
}' "$CONFIG" > /tmp/claude_desktop_config.with-kagent.json \
  && cp /tmp/claude_desktop_config.with-kagent.json "$CONFIG"

jq . "$CONFIG" >/dev/null && echo "JSON OK"
jq '.mcpServers | keys' "$CONFIG"
```

Restart Claude Desktop:

```zsh
osascript -e 'quit app "Claude"'
open -a Claude
```

Watch logs:

```zsh
tail -n 100 -F "$HOME/Library/Logs/Claude/mcp"*.log
```

Filter logs:

```zsh
grep -iE 'kagent-lab|mcp-proxy|error|disconnect|transport' \
  "$HOME/Library/Logs/Claude/mcp"*.log | tail -80
```

### 11.3 Test from Claude

Ask Claude:

```text
Use kagent-lab to list available agents.
```

Expected: `lab-k8s-reader` appears.

Then ask:

```text
Use the lab-k8s-reader kagent agent to list Kubernetes namespaces.
```

Expected result from the working lab:

```text
default
flux-system
kagent
kube-node-lease
kube-public
kube-system
metallb-system
openbao
podinfo
```

---

## Optional — add explicit Kustomize files

The current repo works because Flux points at directories. If a directory has no `kustomization.yaml`, Flux can apply YAMLs in that directory. If you add `kustomization.yaml`, Flux runs Kustomize and only applies listed resources.

This is safer because scratch YAMLs are not applied accidentally.

Add root cluster kustomization:

```zsh
cat > clusters/flux-bao-test/kustomization.yaml <<'EOF'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - flux-instance.yaml
  - infra-controllers.yaml
  - infra-configs.yaml
  - apps.yaml
EOF
```

Add infra controllers kustomization:

```zsh
cat > infrastructure/controllers/kustomization.yaml <<'EOF'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - metallb.yaml
  - openbao.yaml
  - flux-mcp.yaml
  - kagent.yaml
  - ollama.yaml
EOF
```

Add infra configs kustomization:

```zsh
cat > infrastructure/configs/kustomization.yaml <<'EOF'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - metallb-config.yaml
  - traefik-pin.yaml
  - flux-ui-service.yaml
  - flux-web-netpol.yaml
  - flux-mcp-service.yaml
  - flux-mcp-netpol.yaml
  - ingress-flux.yaml
  - ingress-openbao.yaml
  - ingress-kagent.yaml
EOF
```

Add apps kustomization:

```zsh
cat > apps/kustomization.yaml <<'EOF'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - podinfo.yaml
  - podinfo-ingress.yaml
  - kagent-test-agent.yaml
EOF
```

Validate locally:

```zsh
kubectl kustomize clusters/flux-bao-test >/tmp/root.yaml
kubectl kustomize infrastructure/controllers >/tmp/controllers.yaml
kubectl kustomize infrastructure/configs >/tmp/configs.yaml
kubectl kustomize apps >/tmp/apps.yaml
```

Commit:

```zsh
git add \
  clusters/flux-bao-test/kustomization.yaml \
  infrastructure/controllers/kustomization.yaml \
  infrastructure/configs/kustomization.yaml \
  apps/kustomization.yaml

git commit -m "add explicit kustomization files"
git push

flux reconcile source git flux-system
flux reconcile kustomization flux-system
flux reconcile kustomization infra-controllers
flux reconcile kustomization infra-configs
flux reconcile kustomization apps
```

---

# Operations cheat sheet

## Cluster

```zsh
kubectl get nodes -o wide
kubectl get pods -A -o wide
kubectl get pods -A | grep -vE 'Running|Completed'
kubectl get events -A --sort-by=.lastTimestamp | tail -50
kubectl top nodes
kubectl top pods -A
```

## Flux

```zsh
flux get all -A
flux get kustomizations
flux get helmreleases -A
flux get sources all -A
flux stats -A
flux tree kustomization flux-system
flux logs --level=error -A

flux reconcile source git flux-system
flux reconcile kustomization infra-controllers
flux reconcile kustomization infra-configs
flux reconcile kustomization apps
```

## Networking

```zsh
kubectl get svc -A | grep LoadBalancer
kubectl get ingress -A
kubectl -n metallb-system get ipaddresspool,l2advertisement
kubectl -n metallb-system logs -l app.kubernetes.io/component=speaker --tail=50

TRAEFIK_IP=$(kubectl -n kube-system get svc traefik -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
echo "$TRAEFIK_IP"
```

Which service owns an IP:

```zsh
IP=192.168.252.249
kubectl get svc -A -o json | jq -r --arg ip "$IP" '
  .items[]
  | select(.status.loadBalancer.ingress[0].ip == $ip)
  | .metadata.namespace + "/" + .metadata.name
'
```

## OpenBao

```zsh
kubectl -n openbao get pods
kubectl -n openbao exec openbao-0 -- bao status

K1=$(jq -r '.unseal_keys_b64[0]' openbao-init.json)
K2=$(jq -r '.unseal_keys_b64[1]' openbao-init.json)
K3=$(jq -r '.unseal_keys_b64[2]' openbao-init.json)

for P in openbao-0 openbao-1; do
  for K in "$K1" "$K2" "$K3"; do
    kubectl -n openbao exec "$P" -- bao operator unseal "$K"
  done
done

kubectl -n openbao exec openbao-0 -- \
  env BAO_TOKEN=$(jq -r '.root_token' openbao-init.json) bao operator raft list-peers
```

## kagent

```zsh
kubectl -n kagent get pods,svc,ingress
kubectl -n kagent get agents
kubectl -n kagent get modelconfigs,modelproviderconfigs
kubectl -n kagent logs deploy/kagent-controller --tail=100
kubectl -n kagent logs deploy/lab-k8s-reader --tail=100
kubectl -n kagent logs deploy/ollama --tail=100
```

## Claude Desktop MCP logs

```zsh
tail -n 100 -F "$HOME/Library/Logs/Claude/mcp"*.log

grep -iE 'kagent-lab|mcp-proxy|error|disconnect|transport' \
  "$HOME/Library/Logs/Claude/mcp"*.log | tail -100
```

---

# Troubleshooting

## k3s `svclb-*` pods exist

Cause: k3s ServiceLB/klipper was not disabled before install.

Check:

```zsh
kubectl get pods -n kube-system | grep svclb
```

Fix for a clean lab: rebuild k3s with this file present before install:

```yaml
disable:
  - servicelb
```

## Workloads run on control-plane

Cause: control-plane taint missing.

Check:

```zsh
kubectl describe node k3s-cp | grep -iA2 taint
```

Fix:

```zsh
kubectl taint node k3s-cp node-role.kubernetes.io/control-plane=:NoSchedule --overwrite
```

## MetalLB services stay `<pending>`

Check:

```zsh
kubectl -n metallb-system get pods
kubectl -n metallb-system get ipaddresspool,l2advertisement
kubectl -n metallb-system logs -l app.kubernetes.io/component=speaker --tail=100
kubectl get svc -A | grep LoadBalancer
```

Common causes:

- MetalLB pool is not in the Multipass subnet.
- Traefik pinned IP is already leased by another Service.
- MetalLB FRR/K8s mode is enabled instead of plain L2.

The chart values should have:

```yaml
speaker:
  frr:
    enabled: false
frrk8s:
  enabled: false
```

## Traefik hostname goes to wrong place or returns `000`

Regenerate `/etc/hosts`:

```zsh
sudo sed -i '' '/# flux-bao-lab hosts start/,/# flux-bao-lab hosts end/d' /etc/hosts
./scripts/metallb-hosts.zsh | sudo tee -a /etc/hosts >/dev/null

grep -A20 'flux-bao-lab hosts start' /etc/hosts
```

Check Ingress:

```zsh
kubectl get ingress -A
```

All Traefik hostnames should resolve to Traefik's pinned IP.

## Ingress returns `503`

Traefik matched the route, but the backend has no ready endpoints.

Check:

```zsh
kubectl -n <namespace> get pods,svc,endpoints
kubectl -n <namespace> describe pod <pod>
```

For OpenBao, this usually means the pod is sealed.

## `curl http://kagent-mcp.prod1.fmgb.lab/mcp` returns 400

This is good if the body says:

```text
Bad Request: GET requires an Mcp-Session-Id header
```

That means Traefik and kagent routing are working. Bare `curl` is not a complete MCP client.

## kagent MCP list is empty in Claude

Claude says no invokable kagent agents are available.

Check:

```zsh
kubectl -n kagent get agents
kubectl -n kagent get agent lab-k8s-reader -o jsonpath='{range .status.conditions[*]}{.type}={.status} {.reason}{"\n"}{end}'
```

Agent must be:

```text
Accepted=True
Ready=True DeploymentReady
```

## Claude Desktop says `Server disconnected`

First check config JSON:

```zsh
CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
jq . "$CONFIG" >/dev/null && echo "JSON OK"
jq '.mcpServers | keys' "$CONFIG"
```

Common JSON error: missing comma between MCP server blocks.

Good `kagent-lab` config:

```json
"kagent-lab": {
  "command": "uvx",
  "args": [
    "mcp-proxy",
    "http://kagent-mcp.prod1.fmgb.lab/mcp",
    "--transport=streamablehttp"
  ],
  "env": {
    "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
  }
}
```

Restart Claude:

```zsh
osascript -e 'quit app "Claude"'
open -a Claude
```

Logs:

```zsh
grep -iE 'kagent-lab|mcp-proxy|error|disconnect|transport' \
  "$HOME/Library/Logs/Claude/mcp"*.log | tail -100
```

What we learned:

- `mcp-remote` was flaky for this endpoint.
- `mcp-proxy` worked with `--transport=streamablehttp`.
- Wrong argument order caused the bridge to exit early.

## Claude config got overwritten

Quit Claude:

```zsh
osascript -e 'quit app "Claude"'
```

Search VSCodium/VS Code history:

```zsh
grep -R "mcpServers" \
  "$HOME/Library/Application Support/Code/User/History" \
  "$HOME/Library/Application Support/VSCodium/User/History" \
  2>/dev/null | head -50
```

Copy candidates to a recovery folder:

```zsh
RECOVERY="$HOME/Desktop/claude-mcp-recovery-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RECOVERY"

cp "$HOME/Library/Application Support/VSCodium/User/History/"*/*.json "$RECOVERY"/ 2>/dev/null || true
cp "$HOME/Library/Application Support/Code/User/History/"*/*.json "$RECOVERY"/ 2>/dev/null || true

for f in "$RECOVERY"/*.json; do
  echo "===== $f ====="
  jq '.mcpServers | keys' "$f" 2>/dev/null || true
done
```

Restore only after validating:

```zsh
CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
SELECTED_FILE="$RECOVERY/<file>.json"

jq . "$SELECTED_FILE" >/dev/null && cp "$SELECTED_FILE" "$CONFIG"
```

## Ollama pod stuck in `ContainerCreating`

Check:

```zsh
kubectl -n kagent describe pod -l app=ollama
kubectl -n kagent get events --sort-by=.lastTimestamp | tail -30
kubectl -n kagent get pod -l app=ollama -o wide
```

In this lab, the image pull took over 5 minutes because `ollama/ollama` is large. Events showed `Pulled`, `Created`, `Started`, while status was still stale. Deleting the pod fixed it:

```zsh
kubectl -n kagent delete pod -l app=ollama
kubectl -n kagent get pods -w
```

PVC is preserved.

If it repeats:

```zsh
multipass exec k3s-w2 -- df -h
multipass exec k3s-w2 -- free -h
multipass exec k3s-w2 -- sudo dmesg | grep -iE 'oom|killed|memory' | tail -30
multipass exec k3s-w2 -- sudo journalctl -u k3s-agent --since "15 minutes ago" | grep -iE 'ollama|container|sandbox|cni|error|failed' | tail -80
```

## kagent agent cannot connect to Ollama

Check Ollama service and model:

```zsh
kubectl -n kagent run ollama-check --rm -it \
  --image=curlimages/curl \
  --restart=Never \
  -- curl -s http://ollama.kagent.svc.cluster.local:11434/api/tags
```

Check ModelConfig:

```zsh
kubectl -n kagent get modelconfig default-model-config -o yaml
```

Required host:

```text
http://ollama.kagent.svc.cluster.local:11434
```

Restart the agent after changing ModelConfig:

```zsh
kubectl -n kagent rollout restart deploy/lab-k8s-reader
kubectl -n kagent rollout status deploy/lab-k8s-reader
```

## Agent disconnects mid-request

In this lab, the cause was an oversized Ollama context:

```text
num_ctx: "64000"   # bad on 4 GiB CPU-only worker
num_ctx: "4096"    # worked
```

Patch:

```zsh
kubectl -n kagent patch modelconfig default-model-config --type merge -p '{
  "spec": {
    "ollama": {
      "host": "http://ollama.kagent.svc.cluster.local:11434",
      "options": {
        "num_ctx": "4096"
      }
    }
  }
}'
```

Then restart:

```zsh
kubectl -n kagent rollout restart deploy/lab-k8s-reader
kubectl -n kagent rollout status deploy/lab-k8s-reader
```

## OpenBao pod remains `0/1`

Usually sealed.

```zsh
kubectl -n openbao exec openbao-0 -- bao status
```

Unseal with three keys.

## OpenBao pod stuck `Pending` after rescheduling

k3s local-path volumes are node-bound. If a StatefulSet pod must move, the old PVC can pin it to the wrong node.

Lab reset fix:

```zsh
flux suspend helmrelease openbao -n openbao
kubectl -n openbao delete statefulset openbao --cascade=foreground
kubectl -n openbao delete pvc data-openbao-0 data-openbao-1
flux resume helmrelease openbao -n openbao
```

Then reinitialize and unseal. Old data is lost.

## Flux does not apply a new YAML file

If you add explicit `kustomization.yaml` files, every resource must be listed.

Check what Flux manages:

```zsh
flux tree kustomization infra-controllers
flux tree kustomization infra-configs
flux tree kustomization apps
```

Validate local Kustomize build:

```zsh
kubectl kustomize infrastructure/controllers
kubectl kustomize infrastructure/configs
kubectl kustomize apps
```

## GitOps reconcile sequence

After changing manifests:

```zsh
git add .
git commit -m "describe change"
git push

flux reconcile source git flux-system
flux reconcile kustomization infra-controllers
flux reconcile kustomization infra-configs
flux reconcile kustomization apps
```

---

## Teardown and rebuild

```zsh
multipass delete --purge k3s-cp k3s-w1 k3s-w2
rm -f ~/.kube/config
rm -f openbao-init.json openbao-init.*.json
```

Then rerun Steps 2 onward.

---

## Final success criteria

```zsh
kubectl get nodes
flux get all -A
kubectl get pods -A | grep -vE 'Running|Completed'
kubectl get ingress -A
kubectl -n kagent get agents
kubectl -n kagent get modelconfig default-model-config -o yaml
kubectl -n kagent exec deploy/ollama -- ollama list
```

Expected kagent outcome:

```text
Agent lab-k8s-reader: Accepted=True, Ready=True
Ollama model: llama3.2
Claude Desktop prompt works: "Use the lab-k8s-reader kagent agent to list Kubernetes namespaces."
```
