#!/usr/bin/env python3
"""End-to-end A2A test: lab-coordinator must delegate to lab-k8s-reader.

Sends a delegation request to the coordinator over the A2A JSON-RPC protocol,
polls the task until it reaches a terminal state, and verifies that:
  1. the task completes (not `failed`, not stuck in `working`),
  2. the history shows a real round-trip (a specialist response, not just the
     coordinator echoing a tool call as text).

Usage:
    kubectl port-forward -n kagent svc/lab-coordinator 18080:8080 &
    ./test_delegation.py
    ./test_delegation.py --url http://lab-coordinator.kagent:8080  # in-cluster

Exit code: 0 on PASS, 1 on FAIL.
"""

import argparse
import json
import sys
import time
import urllib.request

PROMPT = (
    'Delegate to lab-k8s-reader and ask it only: '
    '"Return exactly one Kubernetes namespace name." Then return its answer.'
)


def rpc(url, method, params, timeout=120):
    body = json.dumps({
        "jsonrpc": "2.0", "id": "1", "method": method, "params": params,
    }).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        out = json.load(resp)
    if "error" in out:
        raise RuntimeError(f"JSON-RPC error: {out['error']}")
    return out["result"]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:18080",
                    help="coordinator A2A URL (default: port-forward on 18080)")
    ap.add_argument("--poll-seconds", type=int, default=10)
    ap.add_argument("--max-wait", type=int, default=600,
                    help="overall deadline; a healthy delegation on CPU "
                         "completes well within this (default 600s)")
    args = ap.parse_args()

    print(f"sending delegation request to {args.url} ...", flush=True)
    task = rpc(args.url, "message/send", {
        "message": {
            "role": "user",
            "messageId": "test-delegation-1",
            "parts": [{"kind": "text", "text": PROMPT}],
        },
    }, timeout=args.max_wait)

    task_id = task["id"]
    state = task["status"]["state"]
    deadline = time.monotonic() + args.max_wait
    while state in ("submitted", "working") and time.monotonic() < deadline:
        time.sleep(args.poll_seconds)
        task = rpc(args.url, "tasks/get", {"id": task_id})
        state = task["status"]["state"]
        print(f"task {task_id}: {state}", flush=True)

    artifacts = [
        p.get("text", "")
        for a in task.get("artifacts", [])
        for p in a.get("parts", [])
    ]
    authors = {
        h.get("metadata", {}).get("kagent_author")
        for h in task.get("history", [])
    }

    print(f"\nfinal state: {state}")
    for text in artifacts:
        print(f"artifact: {text[:200]}")

    failures = []
    if state != "completed":
        failures.append(
            f"task ended in '{state}' (stuck-in-working means the model "
            "returned an empty/unparseable tool call; see test/ai/README.md)")
    # A tool call echoed as text instead of executed looks like:
    #   {"name": "kagent__NS_lab_k8s_reader", "parameters": {...}}
    if any('"parameters"' in t and "kagent" in t for t in artifacts):
        failures.append("artifact contains a tool call printed as text — "
                        "the model failed to emit a structured tool call")
    if "lab_k8s_reader" not in {a for a in authors if a}:
        failures.append("no lab_k8s_reader turn in task history — "
                        "the coordinator never actually delegated")

    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nPASS: coordinator delegated to lab-k8s-reader and completed")
    sys.exit(0)


if __name__ == "__main__":
    main()
