# flux-bao-lab — Full Install and Troubleshooting Runbook

A reproducible GitOps lab on macOS: a three-node k3s cluster on Multipass VMs, bootstrapped with the Flux Operator and driven from this Git repository. It deploys MetalLB, OpenBao, podinfo, Flux Web UI, Flux MCP, kagent, in-cluster Ollama, and two lab kagent agents: `lab-k8s-reader` and `lab-coordinator`.

Every user-facing service is intentionally reachable in two ways:

1. **Direct MetalLB access** through an individual `LoadBalancer` service IP.
2. **Hostname access through Traefik** through one pinned Traefik MetalLB IP.

Tested shape: Apple Silicon macOS, zsh, Multipass VMs, k3s, Flux Operator, MetalLB L2, Traefik, OpenBao, kagent, Ollama, Claude Desktop MCP.

Repository:

```text
https://github.com/d1gital-f/flux-bao-lab
```

---

## 1. Architecture

| Component | Role |
|---|---|
| Multipass | Runs three Ubuntu VMs directly reachable from the Mac |
| k3s | One tainted control-plane and two worker nodes |
| Flux Operator | Bootstraps and manages Flux from this repository |
| Flux Kustomizations | Reconcile `infra-controllers`, `infra-configs`, and `apps` |
| Kustomize files | Explicitly list manifests in each directory |
| MetalLB | Allocates direct `LoadBalancer` IPs from the Multipass subnet |
| Traefik | k3s bundled ingress controller, pinned to one MetalLB IP |
| OpenBao | HA Raft secrets store |
| podinfo | Demo application |
| Flux Web UI | Flux Operator web UI |
| Flux MCP | Flux Operator MCP server |
| kagent | Kubernetes-native agent platform |
| Ollama | In-cluster model backend for kagent |
| Claude Desktop | Local MCP client bridged to kagent MCP with `mcp-proxy` |

Flux reconciliation order:

```text
flux-system -> infra-controllers -> infra-configs -> apps-platform -> apps
```

kagent and Ollama deploy in the `apps-platform` layer so their `LoadBalancer` services are created after the MetalLB pool from `infra-configs` exists. The kagent Helm install waits for LoadBalancer IPs, so it must run after the pool is in place.

`apps-platform` and `apps` are separate Flux Kustomizations because the kagent
HelmReleases install the kagent CRDs, while the Agent and ModelConfig resources
consume those CRDs. Flux server-side dry-runs every resource in a Kustomization
before applying any of them, so CRD providers and CRD consumers cannot share one
Kustomization on a fresh cluster: the consumers fail validation and block the
providers from ever installing. The split plus `dependsOn` guarantees the CRDs
exist before the agents are validated.

Access model:

```text
Direct MetalLB:
  Mac -> service-specific IP:port -> backend Service

Traefik hostname:
  Mac -> hostname -> Traefik IP -> Kubernetes Ingress -> backend Service
```

---

## 2. Repository layout

```text
clusters/flux-bao-test/
  kustomization.yaml        # explicit Kustomize root
  flux-instance.yaml        # FluxInstance pointing Flux at this repo
  infra-controllers.yaml    # Flux Kustomization -> ./infrastructure/controllers
  infra-configs.yaml        # Flux Kustomization -> ./infrastructure/configs
  apps-platform.yaml        # Flux Kustomization -> ./apps/platform (kagent + Ollama)
  apps.yaml                 # Flux Kustomization -> ./apps (depends on apps-platform)

infrastructure/controllers/
  kustomization.yaml        # explicit Kustomize resource list
  metallb.yaml              # MetalLB HelmRepository + HelmRelease
  openbao.yaml              # OpenBao HelmRepository + HelmRelease
  flux-mcp.yaml             # Flux MCP ResourceSet

infrastructure/configs/
  kustomization.yaml        # explicit Kustomize resource list
  metallb-config.yaml       # MetalLB IPAddressPool + L2Advertisement
  traefik-pin.yaml          # pins Traefik LoadBalancer IP
  flux-ui-service.yaml      # direct LB Service for Flux UI
  flux-web-netpol.yaml      # Flux UI network policy
  flux-mcp-service.yaml     # direct LB Service for Flux MCP
  flux-mcp-netpol.yaml      # Flux MCP network policy
  ingress-flux.yaml         # Traefik Ingress for Flux UI + Flux MCP
  ingress-openbao.yaml      # Traefik Ingress for OpenBao UI
  kagent-namespace.yaml     # kagent namespace (needed by ingress-kagent and the apps layer)
  ingress-kagent.yaml       # Traefik Ingress for kagent UI + kagent MCP

apps/platform/
  kustomization.yaml        # explicit Kustomize resource list
  kagent.yaml               # kagent CRDs + kagent HelmReleases
  ollama.yaml               # in-cluster Ollama Deployment/PVC/Service

apps/
  kustomization.yaml        # explicit Kustomize resource list
  podinfo.yaml              # podinfo HelmRepository + HelmRelease + PDB
  podinfo-ingress.yaml      # Traefik Ingress for podinfo
  kagent-modelconfig.yaml   # kagent Ollama ModelConfig, llama3.2, num_ctx=8192
  kagent-reader-agent.yaml  # lab-k8s-reader kagent Agent
  kagent-coordinator-agent.yaml # lab-coordinator kagent Agent

test/ai/
  README.md                 # model benchmark background and workflow
  bench_tool_calls.py       # Ollama tool-calling benchmark (kagent tool name)
  test_delegation.py        # end-to-end coordinator -> reader A2A test

scripts/
  metallb-hosts.zsh         # prints copy/paste /etc/hosts block
  lab-urls.zsh              # prints Ingress and direct LoadBalancer URLs
```

---

## 3. Install local tools

```zsh
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
eval "$(/opt/homebrew/bin/brew shellenv)"

brew install multipass kubernetes-cli helm jq git fluxcd/tap/flux uv
```

Optional:

```zsh
brew install --cask visual-studio-code
# or
brew install --cask vscodium
```

If Multipass requires authentication:

```zsh
multipass authenticate
```

---

## 4. Clone the repo

```zsh
cd ~
git clone https://github.com/d1gital-f/flux-bao-lab.git
cd flux-bao-lab
```

---

## 5. Create the k3s cluster

Launch VMs:

```zsh
multipass launch 24.04 --name k3s-cp --cpus 2 --memory 2G --disk 20G
multipass launch 24.04 --name k3s-w1 --cpus 2 --memory 8G --disk 20G
multipass launch 24.04 --name k3s-w2 --cpus 2 --memory 8G --disk 20G
```

Create server config before installing k3s:

```zsh
multipass exec k3s-cp -- sudo mkdir -p /etc/rancher/k3s
multipass exec k3s-cp -- sudo tee /etc/rancher/k3s/config.yaml >/dev/null <<'K3SCONFIG'
disable:
  - servicelb
node-taint:
  - "node-role.kubernetes.io/control-plane=:NoSchedule"
K3SCONFIG
```

Install k3s server:

```zsh
multipass exec k3s-cp -- bash -c "curl -sfL https://get.k3s.io | sh -"
```

Capture server IP and token:

```zsh
VM_IP=$(multipass info k3s-cp --format json | jq -r '.info["k3s-cp"].ipv4[0]')
TOKEN=$(multipass exec k3s-cp -- sudo cat /var/lib/rancher/k3s/server/node-token)
echo "VM_IP=$VM_IP"
```

Join workers:

```zsh
multipass exec k3s-w1 -- bash -c "curl -sfL https://get.k3s.io | K3S_URL=https://${VM_IP}:6443 K3S_TOKEN=${TOKEN} sh -"
multipass exec k3s-w2 -- bash -c "curl -sfL https://get.k3s.io | K3S_URL=https://${VM_IP}:6443 K3S_TOKEN=${TOKEN} sh -"
```

Install kubeconfig on the Mac:

```zsh
mkdir -p ~/.kube
multipass exec k3s-cp -- sudo cat /etc/rancher/k3s/k3s.yaml | sed "s/127.0.0.1/$VM_IP/" > ~/.kube/config
chmod 600 ~/.kube/config
```

Verify:

```zsh
kubectl get nodes -o wide
kubectl describe node k3s-cp | grep -iA2 taint
kubectl get pods -n kube-system | grep svclb || true
```

Expected:

```text
3 nodes Ready
control-plane tainted NoSchedule
no svclb-* pods
```

---

## 6. Align the MetalLB subnet

The manifests currently use:

```text
192.168.252.0/24
```

Check your Multipass subnet:

```zsh
multipass list | grep k3s
```

Find hard-coded subnet values:

```zsh
grep -R "192\.168\.252" infrastructure clusters apps scripts -n
```

If your subnet differs, retarget before bootstrapping Flux:

```zsh
NEW=192.168.64   # set this to your first three octets

grep -rl '192\.168\.252' infrastructure clusters apps scripts 2>/dev/null \
  | xargs sed -i '' "s/192\.168\.252/${NEW}/g"

git add infrastructure clusters apps scripts
git commit -m "retarget lab subnet to ${NEW}.0/24"
git push
```

Keep the MetalLB pool high in the subnet, for example `.200-.250`.

---

## 7. Bootstrap Flux

Install the Flux Operator imperatively. Everything else is GitOps.

```zsh
helm install flux-operator \
  oci://ghcr.io/controlplaneio-fluxcd/charts/flux-operator \
  --namespace flux-system --create-namespace \
  --set web.enabled=true \
  --set web.networkPolicy.create=false \
  --wait
```

Apply the FluxInstance:

```zsh
kubectl apply -f clusters/flux-bao-test/flux-instance.yaml
```

Watch:

```zsh
flux get kustomizations --watch
```

Force reconcile:

```zsh
flux reconcile source git flux-system
flux reconcile kustomization flux-system
flux reconcile kustomization infra-controllers
flux reconcile kustomization infra-configs
flux reconcile kustomization apps
```

Check:

```zsh
flux get kustomizations
flux get helmreleases -A
kubectl get pods -A -o wide
kubectl get svc -A | grep LoadBalancer
kubectl get ingress -A
```

---

## 8. Kustomize layout and validation

Flux points at directories. Because each directory now contains a `kustomization.yaml`, Kustomize only applies resources explicitly listed there.

Validate before committing:

```zsh
kubectl kustomize clusters/flux-bao-test >/tmp/root.yaml
kubectl kustomize infrastructure/controllers >/tmp/controllers.yaml
kubectl kustomize infrastructure/configs >/tmp/configs.yaml
kubectl kustomize apps >/tmp/apps.yaml
```

Quick loop:

```zsh
for d in clusters/flux-bao-test infrastructure/controllers infrastructure/configs apps; do
  echo "== $d =="
  kubectl kustomize "$d" >/dev/null && echo OK
done
```

If local `kubectl kustomize` fails, Flux will fail too.

---

## 9. Access paths

### 9.1 Direct MetalLB access

List all direct LoadBalancer services:

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

Export useful variables:

```zsh
FLUX_UI_IP=$(kubectl -n flux-system get svc flux-operator-ui -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
FLUX_MCP_IP=$(kubectl -n flux-system get svc flux-operator-mcp-lb -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
BAO_IP=$(kubectl -n openbao get svc openbao-ui -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
PODINFO_IP=$(kubectl -n podinfo get svc podinfo -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
KAGENT_UI_IP=$(kubectl -n kagent get svc kagent-ui -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
KAGENT_MCP_IP=$(kubectl -n kagent get svc kagent-controller -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
TRAEFIK_IP=$(kubectl -n kube-system get svc traefik -o jsonpath='{.status.loadBalancer.ingress[0].ip}')

cat <<URLS
Flux UI direct:    http://${FLUX_UI_IP}:9080
Flux MCP direct:   http://${FLUX_MCP_IP}:9090/mcp
OpenBao direct:    http://${BAO_IP}:8200/ui
podinfo direct:    http://${PODINFO_IP}:9898
kagent UI direct:  http://${KAGENT_UI_IP}:8080
kagent MCP direct: http://${KAGENT_MCP_IP}:8083/mcp
Traefik IP:        ${TRAEFIK_IP}
URLS
```

### 9.2 Traefik hostname access

Traefik is pinned by `infrastructure/configs/traefik-pin.yaml`.

Check:

```zsh
kubectl -n kube-system get svc traefik
kubectl get ingress -A
```

Expected hostnames:

```text
fluxui.prod1.fmgb.lab
fluxmcp.prod1.fmgb.lab
openbao.prod1.fmgb.lab
podinfo.prod1.fmgb.lab
kagent.prod1.fmgb.lab
kagent-mcp.prod1.fmgb.lab
```

Generate copy/paste `/etc/hosts` content:

```zsh
./scripts/metallb-hosts.zsh
```

Apply safely:

```zsh
sudo sed -i '' '/# flux-bao-lab hosts start/,/# flux-bao-lab hosts end/d' /etc/hosts
./scripts/metallb-hosts.zsh | sudo tee -a /etc/hosts >/dev/null
```

Verify:

```zsh
grep -A20 '# flux-bao-lab hosts start' /etc/hosts
```

The script includes both:

```text
Traefik hostnames:
  *.prod1.fmgb.lab names explicitly listed by Ingress resources

Direct MetalLB hostnames:
  <service>.<namespace>.direct.fmgb.lab
```

For real DNS, `/etc/hosts` cannot use wildcards. A DNS zone can use this for the Traefik path:

```dns
*.prod1.fmgb.lab. 300 IN A <TRAEFIK_IP>
```

Direct MetalLB service hostnames need explicit A records per service IP.

---

## 10. OpenBao initialization and unseal

OpenBao starts sealed. Initialize pod 0, unseal it, wait for pod 1, then unseal pod 1.

Initialize safely:

```zsh
kubectl -n openbao exec openbao-0 -- \
  bao operator init -key-shares=5 -key-threshold=3 -format=json > openbao-init.new.json \
  && mv openbao-init.new.json openbao-init.json

jq . openbao-init.json
```

Extract keys:

```zsh
K1=$(jq -r '.unseal_keys_b64[0]' openbao-init.json)
K2=$(jq -r '.unseal_keys_b64[1]' openbao-init.json)
K3=$(jq -r '.unseal_keys_b64[2]' openbao-init.json)
```

Unseal pod 0:

```zsh
for K in "$K1" "$K2" "$K3"; do
  kubectl -n openbao exec openbao-0 -- bao operator unseal "$K"
done
```

Wait for pod 1:

```zsh
kubectl -n openbao get pods -w
```

Unseal pod 1:

```zsh
for K in "$K1" "$K2" "$K3"; do
  kubectl -n openbao exec openbao-1 -- bao operator unseal "$K"
done
```

Verify HA:

```zsh
kubectl -n openbao exec openbao-0 -- bao status
kubectl -n openbao exec openbao-0 -- \
  env BAO_TOKEN=$(jq -r '.root_token' openbao-init.json) bao operator raft list-peers
```

Back up keys outside the repo:

```zsh
cp openbao-init.json ~/openbao-init.backup.json
```

---

## 11. kagent, Ollama, and Claude Desktop MCP

### 11.1 kagent install

`apps/platform/kagent.yaml` installs kagent through Helm and exposes both the UI and MCP/API path through direct MetalLB services. It lives in the `apps-platform` layer so its `LoadBalancer` services are created after the MetalLB pool from `infra-configs` exists, and so its CRDs are installed before the `apps` layer applies the Agent and ModelConfig resources:

| Item | Purpose |
|---|---|
| `kagent-crds` HelmRelease | Installs the kagent CRDs |
| `kagent` HelmRelease | Installs the kagent controller, UI, tools, PostgreSQL, and kmcp components |
| `kagent-controller` Service | Exposes kagent MCP/API access as a direct MetalLB `LoadBalancer` |
| `kagent-ui` Service | Exposes the kagent UI as a direct MetalLB `LoadBalancer` |
| `kagent-tools` | Enables the in-cluster MCP tool server used by agents |
| `kmcp` | Enables kagent MCP integration |
| Packaged sample agents | Disabled; this lab defines its own agents in `apps/` |

Verify:

```zsh
flux get helmreleases -n kagent
kubectl -n kagent get pods,svc
kubectl get crd | grep kagent
```

Expected pods include:

```text
kagent-controller
kagent-kmcp-controller-manager
kagent-postgresql
kagent-tools
kagent-ui
```

### 11.2 kagent Traefik routes

Configured by `infrastructure/configs/ingress-kagent.yaml`:

```text
http://kagent.prod1.fmgb.lab      -> kagent-ui:8080
http://kagent-mcp.prod1.fmgb.lab  -> kagent-controller:8083/mcp
```

Check:

```zsh
kubectl -n kagent get ingress
curl -I http://kagent.prod1.fmgb.lab
curl -sv http://kagent-mcp.prod1.fmgb.lab/mcp 2>&1 | head -30
```

A bare curl to `/mcp` may return:

```text
Bad Request: GET requires an Mcp-Session-Id header
```

That means the route reaches kagent. It is not a failure.

### 11.3 Ollama

Ollama is installed by `apps/platform/ollama.yaml`:

```text
Deployment:  ollama
Image:       ollama/ollama:0.30.7
PVC:         ollama-data, 10Gi
Service:     ollama.kagent.svc.cluster.local:11434
Model:       llama3.2 pulled by postStart hook
Concurrency: one loaded model and one parallel request to reduce RAM spikes
```

Verify:

```zsh
kubectl -n kagent get pods,pvc,svc | grep ollama
kubectl -n kagent logs deploy/ollama --tail=50
kubectl -n kagent exec deploy/ollama -- ollama list
```

If needed:

```zsh
kubectl -n kagent exec deploy/ollama -- ollama pull llama3.2
```

Test API from inside the cluster:

```zsh
kubectl -n kagent run ollama-check --rm -it \
  --image=curlimages/curl \
  --restart=Never \
  -- curl -s http://ollama.kagent.svc.cluster.local:11434/api/tags
```

Direct model test:

```zsh
kubectl -n kagent exec deploy/ollama -- \
  ollama run llama3.2 "Reply with only: ok"
```

### 11.4 kagent ModelConfig

The model config is GitOps-managed in:

```text
apps/kagent-modelconfig.yaml
```

It must be:

```yaml
apiVersion: kagent.dev/v1alpha2
kind: ModelConfig
metadata:
  name: lab-ollama-model-config
  namespace: kagent
spec:
  model: llama3.2
  provider: Ollama
  ollama:
    host: http://ollama.kagent.svc.cluster.local:11434
    options:
      num_ctx: "8192"
```

`num_ctx: "8192"` is important. `64000` caused agent disconnects/timeouts on the small CPU-only Multipass VM.

Verify:

```zsh
kubectl -n kagent get modelconfig lab-ollama-model-config -o yaml
```

### 11.5 kagent reader agent

The reader agent is in:

```text
apps/kagent-reader-agent.yaml
```

It is named:

```text
lab-k8s-reader
```

It must reference:

```yaml
modelConfig: lab-ollama-model-config
```

Verify:

```zsh
kubectl -n kagent get agents
kubectl -n kagent get agent lab-k8s-reader -o jsonpath='{.spec.declarative.modelConfig}{"\n"}'
kubectl -n kagent get agent lab-k8s-reader -o jsonpath='{range .status.conditions[*]}{.type}={.status} {.reason}{"\n"}{end}'
```

Expected:

```text
lab-ollama-model-config
Accepted=True Reconciled
Ready=True DeploymentReady
```

---

## 12. Claude Desktop MCP configuration

Claude Desktop local MCP config lives here:

```text
~/Library/Application Support/Claude/claude_desktop_config.json
```

Do not replace the whole file. Merge the `kagent-lab` entry.

Install `uv`:

```zsh
brew install uv
which uvx
```

Back up and validate the config:

```zsh
CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
mkdir -p "$(dirname "$CONFIG")"
[ -f "$CONFIG" ] || echo '{"mcpServers":{}}' > "$CONFIG"
cp "$CONFIG" "$CONFIG.bak.$(date +%Y%m%d-%H%M%S)"
jq . "$CONFIG" >/dev/null && echo "JSON OK"
```

Merge the working bridge:

```zsh
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
}' "$CONFIG" > /tmp/claude_desktop_config.json \
  && cp /tmp/claude_desktop_config.json "$CONFIG"

jq '.mcpServers | keys' "$CONFIG"
```

Restart Claude:

```zsh
osascript -e 'quit app "Claude"'
open -a Claude
```

Watch logs:

```zsh
tail -n 120 -F "$HOME/Library/Logs/Claude/mcp"*.log
```

Test in Claude:

```text
Use kagent-lab to list available agents.
```

Then:

```text
Use the lab-k8s-reader kagent agent to list Kubernetes namespaces.
```

Known working output should include:

```text
default, flux-system, kagent, kube-node-lease, kube-public, kube-system, metallb-system, openbao, podinfo
```

---

## 13. Day-two commands

Cluster:

```zsh
kubectl get nodes -o wide
kubectl get pods -A -o wide
kubectl get pods -A | grep -vE 'Running|Completed'
kubectl get events -A --sort-by=.lastTimestamp | tail -50
kubectl top nodes
kubectl top pods -A
```

Flux:

```zsh
flux get all -A
flux get kustomizations
flux get helmreleases -A
flux get sources all -A
flux logs --level=error -A
flux tree kustomization flux-system
flux tree kustomization infra-controllers
flux tree kustomization infra-configs
flux tree kustomization apps
```

Reconcile:

```zsh
flux reconcile source git flux-system
flux reconcile kustomization flux-system
flux reconcile kustomization infra-controllers
flux reconcile kustomization infra-configs
flux reconcile kustomization apps
```

Networking:

```zsh
kubectl get svc -A | grep LoadBalancer
kubectl get ingress -A
kubectl -n metallb-system get ipaddresspool,l2advertisement
kubectl -n kube-system get svc traefik -o wide
```

Find service owning an IP:

```zsh
IP=192.168.252.249
kubectl get svc -A -o json | jq -r --arg ip "$IP" '
  .items[]
  | select(.status.loadBalancer.ingress[0].ip == $ip)
  | .metadata.namespace + "/" + .metadata.name
'
```

kagent:

```zsh
kubectl -n kagent get pods,svc,ingress
kubectl -n kagent get agents,modelconfigs,remotemcpservers
kubectl -n kagent logs deploy/kagent-controller --tail=100
kubectl -n kagent logs deploy/lab-k8s-reader --tail=100
kubectl -n kagent logs deploy/ollama --tail=100
```

---

## 14. Troubleshooting

### Flux `infra-configs` fails with missing file

Symptom:

```text
kustomize build failed: accumulating resources from 'ingress-flux.yaml': no such file or directory
```

Cause: `kustomization.yaml` references a file that was renamed or not committed.

Fix:

```zsh
ls infrastructure/configs
cat infrastructure/configs/kustomization.yaml
kubectl kustomize infrastructure/configs
```

Make every listed `resources:` file exist, then:

```zsh
git add infrastructure/configs/kustomization.yaml infrastructure/configs
git commit -m "fix infra configs kustomization"
git push
flux reconcile source git flux-system
flux reconcile kustomization infra-configs
```

### A manifest exists but Flux does not apply it

With explicit Kustomize, files must be listed in the directory `kustomization.yaml`.

```zsh
grep -R "your-file.yaml" infrastructure apps clusters
kubectl kustomize apps | grep -i modelconfig
flux tree kustomization apps | grep -i modelconfig
```

### `apps` fails with `no matches for kind "Agent"` or `"ModelConfig"` on first boot

Symptom:

```text
Agent/kagent/lab-k8s-reader dry-run failed: no matches for kind "Agent" in version "kagent.dev/v1alpha2"
```

This is expected on a clean bootstrap. The `apps` Kustomization applies the
`Agent` and `ModelConfig` custom resources in the same reconcile that installs
the `kagent-crds` HelmRelease, so the first attempt runs before the CRDs are
registered. It self-heals on the next retry (`retryInterval: 1m`) once the
CRDs chart has installed. Only investigate if it still fails after the kagent
HelmReleases are Ready:

```zsh
flux get helmreleases -n kagent
kubectl get crd | grep kagent
flux reconcile kustomization apps
```

### `svclb-*` pods exist

k3s ServiceLB was not disabled before install.

```zsh
kubectl get pods -n kube-system | grep svclb
```

Rebuild with:

```yaml
disable:
  - servicelb
```

### Workloads land on control-plane

```zsh
kubectl taint node k3s-cp node-role.kubernetes.io/control-plane=:NoSchedule --overwrite
```

### MetalLB service stays `<pending>`

Check:

```zsh
kubectl -n metallb-system get pods
kubectl -n metallb-system get ipaddresspool,l2advertisement
kubectl -n metallb-system logs -l app.kubernetes.io/component=speaker --tail=80
kubectl get svc -A | grep LoadBalancer
```

Likely causes:

```text
pool subnet wrong
pinned IP already allocated
MetalLB not ready
k3s ServiceLB still running
```

### Traefik hostname wrong, stale, or returns `000`

Regenerate `/etc/hosts`:

```zsh
sudo sed -i '' '/# flux-bao-lab hosts start/,/# flux-bao-lab hosts end/d' /etc/hosts
./scripts/metallb-hosts.zsh | sudo tee -a /etc/hosts >/dev/null
```

Verify:

```zsh
dscacheutil -q host -a name kagent.prod1.fmgb.lab
kubectl get ingress -A
```

### Ingress returns `503`

Backend has no ready endpoints.

```zsh
kubectl get ingress -A
kubectl -n <namespace> get svc,endpoints,pods
```

For OpenBao, this usually means sealed pods.

### Bare curl to kagent MCP returns `400 Mcp-Session-Id`

This is expected and proves routing reaches kagent:

```zsh
curl -sv http://kagent-mcp.prod1.fmgb.lab/mcp 2>&1 | head -30
```

### Claude Desktop MCP config is broken

Validate JSON:

```zsh
CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
jq . "$CONFIG" >/dev/null && echo "JSON OK"
```

Recover from VS Code/VSCodium history:

```zsh
grep -R "mcpServers" \
  "$HOME/Library/Application Support/Code/User/History" \
  "$HOME/Library/Application Support/VSCodium/User/History" \
  2>/dev/null | head -50
```

### Claude says `kagent-lab: Server disconnected`

Check logs:

```zsh
grep -iE 'kagent-lab|mcp-proxy|error|disconnect|transport' \
  "$HOME/Library/Logs/Claude/mcp"*.log | tail -100
```

Use the working `mcp-proxy` config, not the earlier failing `mcp-remote` config.

### Claude lists no agents

```zsh
kubectl -n kagent get agents
kubectl -n kagent get agent lab-k8s-reader -o jsonpath='{range .status.conditions[*]}{.type}={.status} {.reason}{"\n"}{end}'
```

Only accepted and deployment-ready agents are invokable.

### Agent cannot connect to Ollama

```zsh
kubectl -n kagent get modelconfig lab-ollama-model-config -o yaml
kubectl -n kagent get svc ollama
kubectl -n kagent run ollama-check --rm -it \
  --image=curlimages/curl \
  --restart=Never \
  -- curl -s http://ollama.kagent.svc.cluster.local:11434/api/tags
```

Expected: `llama3.2` is listed.

### Agent disconnects mid-request

Likely context too large or model too slow. Lower the context window in
`apps/kagent-modelconfig.yaml`, for example:

```yaml
options:
  num_ctx: "4096"
```

Then:

```zsh
flux reconcile kustomization apps
kubectl -n kagent rollout restart deploy/lab-k8s-reader
kubectl -n kagent rollout status deploy/lab-k8s-reader
```

### Ollama stuck `ContainerCreating`

```zsh
kubectl -n kagent describe pod -l app=ollama
kubectl -n kagent get events --sort-by=.lastTimestamp | tail -30
```

If the image pulled and the container started but status is stale:

```zsh
kubectl -n kagent delete pod -l app=ollama
kubectl -n kagent get pods -w
```

Check node pressure:

```zsh
kubectl top nodes
multipass info k3s-w2
multipass exec k3s-w2 -- df -h
multipass exec k3s-w2 -- free -h
multipass exec k3s-w2 -- sudo dmesg | grep -iE 'oom|killed|memory' | tail -30
```

### OpenBao pod is `0/1`

It is probably sealed. Re-unseal:

```zsh
K1=$(jq -r '.unseal_keys_b64[0]' openbao-init.json)
K2=$(jq -r '.unseal_keys_b64[1]' openbao-init.json)
K3=$(jq -r '.unseal_keys_b64[2]' openbao-init.json)

for P in openbao-0 openbao-1; do
  for K in "$K1" "$K2" "$K3"; do
    kubectl -n openbao exec "$P" -- bao operator unseal "$K"
  done
done
```

### OpenBao pod stuck pending after reschedule

k3s local-path volumes are node-local. In a lab reset:

```zsh
flux suspend helmrelease openbao -n openbao
kubectl -n openbao delete statefulset openbao --cascade=foreground
kubectl -n openbao delete pvc data-openbao-0 data-openbao-1
flux resume helmrelease openbao -n openbao
```

Then reinitialize and unseal.

---

## 15. Teardown

```zsh
multipass delete --purge k3s-cp k3s-w1 k3s-w2
rm -f ~/.kube/config
```

Optional cleanup:

```zsh
rm -f openbao-init.json openbao-init.new.json
```

---

## 16. Known working end state

```text
Flux Kustomizations:
  flux-system=True
  infra-controllers=True
  infra-configs=True
  apps=True

MetalLB and Traefik:
  all exposed services have direct LoadBalancer IPs
  Traefik has one pinned LoadBalancer IP
  all Ingress hostnames resolve through Traefik

kagent:
  kagent HelmRelease=True
  kagent-crds HelmRelease=True
  lab-k8s-reader Accepted=True Ready=True
  lab-coordinator Accepted=True Ready=True
  lab-ollama-model-config Accepted=True

Ollama:
  ollama pod Running
  llama3.2 available
  num_ctx=8192

Claude Desktop:
  kagent-lab MCP bridge uses uvx mcp-proxy
  kagent-lab can list agents
  lab-k8s-reader can list Kubernetes namespaces
```

---

## 17. Latest repo addendum: coordinator, llama3.2, and stable Ollama settings

This section records the current coordinator-specific repository state and validation steps. The main install flow above already reflects the current `llama3.2` model configuration.

### 17.1 Current kagent agents

The current `apps/` Kustomize bundle includes both the Kubernetes reader specialist and the coordinator agent:

```text
apps/
  kagent-reader-agent.yaml        # lab-k8s-reader
  kagent-coordinator-agent.yaml   # lab-coordinator
```

Verify the files are listed:

```zsh
cat apps/kustomization.yaml
```

Expected resources include:

```yaml
resources:
  - podinfo.yaml
  - podinfo-ingress.yaml
  - kagent-reader-agent.yaml
  - kagent-coordinator-agent.yaml
  - kagent-modelconfig.yaml
```

`kagent.yaml` and `ollama.yaml` live in `apps/platform/` with their own
Kustomize list, reconciled by the `apps-platform` Flux Kustomization before
`apps` (see section 1: CRD providers and consumers must be split).

Expected agents after reconciliation:

```zsh
kubectl -n kagent get agents
```

Expected names:

```text
lab-k8s-reader
lab-coordinator
```

### 17.2 Agent roles

```text
lab-coordinator
  - Claude-facing entrypoint agent
  - owns the plan
  - delegates live Kubernetes fact gathering to lab-k8s-reader
  - synthesizes the final answer
  - must stay read-only

lab-k8s-reader
  - specialist read-only Kubernetes fact-gathering agent
  - uses kagent-tools MCP to query the Kubernetes API
  - returns factual cluster state to the coordinator or to Claude
```

Current intended flow:

```text
Claude Desktop
  -> kagent-lab MCP bridge
  -> kagent-controller /mcp
  -> lab-coordinator
  -> lab-k8s-reader
  -> kagent-tools MCP
  -> Kubernetes API
```

Direct reader flow remains valid for troubleshooting:

```text
Claude Desktop
  -> kagent-lab MCP bridge
  -> kagent-controller /mcp
  -> lab-k8s-reader
  -> kagent-tools MCP
  -> Kubernetes API
```

### 17.3 Current Ollama model config

The current `apps/kagent-modelconfig.yaml` uses `llama3.2`:

```yaml
apiVersion: kagent.dev/v1alpha2
kind: ModelConfig
metadata:
  name: lab-ollama-model-config
  namespace: kagent
spec:
  model: llama3.2
  provider: Ollama
  ollama:
    host: http://ollama.kagent.svc.cluster.local:11434
    options:
      num_ctx: "8192"
```

Verify after reconciliation:

```zsh
kubectl -n kagent get modelconfig lab-ollama-model-config -o yaml
kubectl -n kagent get agent lab-k8s-reader -o jsonpath='{.spec.declarative.modelConfig}{"\n"}'
kubectl -n kagent get agent lab-coordinator -o jsonpath='{.spec.declarative.modelConfig}{"\n"}'
```

Expected for both agents:

```text
lab-ollama-model-config
```

### 17.4 Current Ollama deployment behavior

The current `apps/platform/ollama.yaml` pins the Ollama image and pulls `llama3.2` through the container lifecycle hook:

```text
image: ollama/ollama:0.30.7
model pulled: llama3.2
```

It also limits concurrency to reduce memory spikes on the small Multipass worker nodes:

```yaml
env:
  - name: OLLAMA_MAX_LOADED_MODELS
    value: "1"
  - name: OLLAMA_NUM_PARALLEL
    value: "1"
  - name: OLLAMA_KEEP_ALIVE
    value: "5m"
```

Verify model availability:

```zsh
kubectl -n kagent exec deploy/ollama -- ollama list
```

Expected model:

```text
llama3.2
```

If the model is missing:

```zsh
kubectl -n kagent exec deploy/ollama -- ollama pull llama3.2
```

Test the model API from inside the cluster:

```zsh
kubectl -n kagent run ollama-check --rm -it \
  --image=curlimages/curl \
  --restart=Never \
  -- curl -s http://ollama.kagent.svc.cluster.local:11434/api/tags
```

### 17.5 Coordinator validation

After Flux applies the apps Kustomization:

```zsh
kubectl -n kagent get agent lab-coordinator -o jsonpath='{range .status.conditions[*]}{.type}={.status} {.reason}{"\n"}{end}'
kubectl -n kagent get deploy lab-coordinator
kubectl -n kagent logs deploy/lab-coordinator --tail=80
```

Expected conditions:

```text
Accepted=True Reconciled
Ready=True DeploymentReady
```

Basic Claude test:

```text
Use kagent-lab to list available agents.
```

Expected agents include:

```text
lab-k8s-reader
lab-coordinator
```

Coordinator test:

```text
Use the lab-coordinator kagent agent. Delegate to lab-k8s-reader to list Kubernetes namespaces, then summarize only the returned namespace names.
```

If coordinator delegation is unreliable, test the direct reader path:

```text
Use the lab-k8s-reader kagent agent to list Kubernetes namespaces.
```

### 17.6 Known caveat: local small-model tool calling

The local Ollama model is intentionally lightweight for a laptop lab. It can be slower and less reliable than a hosted frontier model for structured tool calling and multi-agent delegation.

Useful diagnosis signals:

```zsh
kubectl -n kagent logs deploy/lab-coordinator --since=10m
kubectl -n kagent logs deploy/lab-k8s-reader --since=10m
kubectl -n kagent logs deploy/ollama --since=10m
kubectl -n kagent get pods -o custom-columns=NAME:.metadata.name,READY:.status.containerStatuses[*].ready,RESTARTS:.status.containerStatuses[*].restartCount --no-headers | grep -E 'lab-k8s-reader|lab-coordinator|ollama'
```

If Ollama restarts, check for OOM kills:

```zsh
kubectl -n kagent describe pod -l app=ollama | grep -iE 'reason|oom|killed|exit|memory|cpu|restart' -A3 -B3
```

The three-Multipass-VM topology is fine for this lab. The resource-sensitive part is the Ollama worker pod, especially if nested coordinator-to-specialist requests are running on CPU only.

---

## 18. Clean rebuild from this repository

Use this section when you want to wipe the lab cluster completely and recreate it from Git.

### 18.1 Pre-wipe checks

Run these before deleting VMs:

```zsh
git status --short
git log --oneline -3
kubectl kustomize clusters/flux-bao-test >/tmp/root.yaml
kubectl kustomize infrastructure/controllers >/tmp/controllers.yaml
kubectl kustomize infrastructure/configs >/tmp/configs.yaml
kubectl kustomize apps >/tmp/apps.yaml
```

If `git status --short` shows uncommitted changes you want to preserve:

```zsh
git add .
git commit -m "update lab runbook and manifests"
git push
```

Confirm the latest pushed repository contains the coordinator and qwen model config:

```zsh
grep -n 'kagent-coordinator-agent.yaml' apps/kustomization.yaml
grep -n 'llama3.2' apps/kagent-modelconfig.yaml apps/platform/ollama.yaml
```

### 18.2 Wipe the cluster

This destroys the three Multipass VMs and all cluster-local data, including OpenBao Raft data, local-path PVCs, Ollama PVC contents, and all Kubernetes state.

```zsh
multipass delete --purge k3s-cp k3s-w1 k3s-w2
rm -f ~/.kube/config
```

Optional local cleanup:

```zsh
rm -f openbao-init.json openbao-init.new.json
```

### 18.3 Recreate from scratch

Start again from the install flow in this runbook:

```text
3. Install local tools
4. Clone the repo
5. Create the k3s cluster
6. Align the MetalLB subnet
7. Bootstrap Flux
8. Kustomize layout and validation
9. Access paths
10. OpenBao initialization and unseal
11. kagent, Ollama, and Claude Desktop MCP
12. Claude Desktop MCP configuration
```

The critical rebuild sequence is:

```zsh
cd ~
git clone https://github.com/d1gital-f/flux-bao-lab.git
cd flux-bao-lab

multipass launch 24.04 --name k3s-cp --cpus 2 --memory 2G --disk 20G
multipass launch 24.04 --name k3s-w1 --cpus 2 --memory 8G --disk 20G
multipass launch 24.04 --name k3s-w2 --cpus 2 --memory 8G --disk 20G
```

Then follow section 5 exactly to install k3s, section 6 to retarget the subnet if needed, and section 7 to bootstrap Flux.

### 18.4 Post-rebuild acceptance checks

Flux:

```zsh
flux get kustomizations
flux get helmreleases -A
```

Expected:

```text
flux-system=True
infra-controllers=True
infra-configs=True
apps=True
```

Networking:

```zsh
kubectl get svc -A | grep LoadBalancer
kubectl get ingress -A
./scripts/metallb-hosts.zsh
```

Ollama and kagent:

```zsh
kubectl -n kagent get pods,svc,ingress
kubectl -n kagent get agents,modelconfigs
kubectl -n kagent exec deploy/ollama -- ollama list
kubectl -n kagent get agent lab-k8s-reader -o jsonpath='{range .status.conditions[*]}{.type}={.status} {.reason}{"\n"}{end}'
kubectl -n kagent get agent lab-coordinator -o jsonpath='{range .status.conditions[*]}{.type}={.status} {.reason}{"\n"}{end}'
```

Expected:

```text
llama3.2 available in Ollama
lab-ollama-model-config present
lab-k8s-reader Accepted=True Ready=True
lab-coordinator Accepted=True Ready=True
```

OpenBao:

```zsh
kubectl -n openbao get pods
kubectl -n openbao exec openbao-0 -- bao status
```

If sealed, use section 10 to initialize or unseal.

Claude Desktop:

```text
Use kagent-lab to list available agents.
```

Expected:

```text
lab-k8s-reader
lab-coordinator
```

Then test direct reader first:

```text
Use the lab-k8s-reader kagent agent to list Kubernetes namespaces.
```

Then test the coordinator:

```text
Use the lab-coordinator kagent agent. Delegate to lab-k8s-reader to list Kubernetes namespaces, then summarize only the returned namespace names.
```
