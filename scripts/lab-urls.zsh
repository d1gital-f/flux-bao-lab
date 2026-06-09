#!/usr/bin/env zsh
set -euo pipefail

DIRECT_DOMAIN="${DIRECT_DOMAIN:-direct.fmgb.lab}"

echo "# Ingress URLs (Traefik, port 80)"
kubectl get ingress -A -o json | jq -r '
  .items[].spec.rules[]?.host // empty
  | "http://\(.)"
' | sort -u

echo ""
echo "# Direct LoadBalancer URLs (MetalLB)"
kubectl get svc -A -o json | jq -r --arg domain "$DIRECT_DOMAIN" '
  .items[]
  | select(.spec.type == "LoadBalancer")
  | select(.status.loadBalancer.ingress[0].ip != null)
  | . as $s
  | .spec.ports[]
  | "http://\($s.metadata.name).\($s.metadata.namespace).\($domain):\(.port)  # \($s.status.loadBalancer.ingress[0].ip)"
' | sort
