#!/usr/bin/env zsh
set -euo pipefail

LAB_DOMAIN="${LAB_DOMAIN:-prod1.fmgb.lab}"
DIRECT_DOMAIN="${DIRECT_DOMAIN:-direct.fmgb.lab}"
TRAEFIK_NS="${TRAEFIK_NS:-kube-system}"
TRAEFIK_SVC="${TRAEFIK_SVC:-traefik}"

TRAEFIK_IP="$(
  kubectl -n "$TRAEFIK_NS" get svc "$TRAEFIK_SVC" \
    -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
)"

INGRESS_HOSTS="$(
  kubectl get ingress -A -o json \
  | jq -r '.items[].spec.rules[]?.host // empty' \
  | sort -u \
  | tr '\n' ' ' \
  | sed 's/[[:space:]]*$//'
)"

cat <<EOF_HOSTS
# flux-bao-lab hosts start
${TRAEFIK_IP}  ${INGRESS_HOSTS}
EOF_HOSTS

kubectl get svc -A -o json | jq -r --arg domain "$DIRECT_DOMAIN" '
  .items[]
  | select(.spec.type == "LoadBalancer")
  | select(.status.loadBalancer.ingress[0].ip != null)
  | "\(.status.loadBalancer.ingress[0].ip)  \(.metadata.name).\(.metadata.namespace).\($domain)"
' | sort

cat <<EOF_HOSTS
# flux-bao-lab hosts end
EOF_HOSTS
