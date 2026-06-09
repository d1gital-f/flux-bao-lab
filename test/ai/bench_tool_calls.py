#!/usr/bin/env python3
"""Benchmark Ollama models on kagent-style tool calling.

kagent exposes sub-agents as tools named like `kagent__NS__lab_k8s_reader`.
Small models often mangle that name or emit the call as plain text; Ollama
then drops the response entirely (empty message, no tool_calls), which leaves
the kagent ADK runner closing silently and the A2A task stuck in `working`.

This benchmark reproduces the production setup as closely as possible:
the real coordinator system prompt + the exact kagent tool name. A model is
only usable for delegation if it scores OK on (nearly) every trial.

Usage:
    kubectl port-forward -n kagent svc/ollama 21434:11434 &
    ./bench_tool_calls.py --models llama3.2 qwen3:4b --trials 5
    ./bench_tool_calls.py --url http://ollama.kagent.svc.cluster.local:11434  # in-cluster

Exit code: 0 if every tested model scored 100% OK, 1 otherwise.
"""

import argparse
import json
import sys
import urllib.request

TOOL_NAME = "kagent__NS__lab_k8s_reader"

# Keep in sync with apps/kagent-coordinator-agent.yaml (spec.declarative.systemMessage).
# Pass --system-from-cluster to use the live agent's prompt instead.
SYSTEM_PROMPT = """You are the lab coordinator agent.

Your job is to own the plan, delegate factual cluster inspection to specialist agents,
and synthesize the final answer.

Current specialist agents:
- lab-k8s-reader: read-only Kubernetes fact-gathering agent.

Rules:
- For live Kubernetes state, delegate to lab-k8s-reader.
- Do not invent Kubernetes objects.
- Do not answer live cluster questions from memory.
- Keep the plan short and explicit.
- When delegating, tell the specialist exactly what facts you need.
- Synthesize the answer only from returned specialist results.
- Do not perform destructive or mutating actions.

Response format:
1. Plan
2. Specialist findings
3. Answer"""

USER_PROMPT = (
    'Delegate to lab-k8s-reader and ask it only: '
    '"Return exactly one Kubernetes namespace name." Then return its answer.'
)

TOOLS = [{
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": "Read-only Kubernetes lab assistant.",
        "parameters": {
            "type": "object",
            "properties": {"request": {"type": "string"}},
            "required": ["request"],
        },
    },
}]


def system_prompt_from_cluster():
    """Fetch the live coordinator system prompt so the bench can't drift."""
    import subprocess
    out = subprocess.run(
        ["kubectl", "-n", "kagent", "get", "agent", "lab-coordinator",
         "-o", "jsonpath={.spec.declarative.systemMessage}"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout


def classify(message):
    """Map an /api/chat response message to a verdict."""
    tool_calls = message.get("tool_calls")
    content = (message.get("content") or "").strip()
    if tool_calls:
        name = tool_calls[0]["function"]["name"]
        return "OK" if name == TOOL_NAME else f"WRONGNAME({name})"
    if content:
        return "TEXT"
    return "EMPTY"


def run_trial(url, model, system, num_ctx, timeout):
    body = json.dumps({
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": USER_PROMPT},
        ],
        "tools": TOOLS,
        "options": {"num_ctx": num_ctx},
        # Suppress reasoning on thinking-capable models (qwen3 etc.); other
        # models ignore the field.
        "think": False,
    }).encode()
    req = urllib.request.Request(
        f"{url}/api/chat", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return classify(json.load(resp)["message"])


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:21434",
                    help="Ollama base URL (default: port-forward on 21434)")
    ap.add_argument("--models", nargs="+", required=True)
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--num-ctx", type=int, default=8192)
    ap.add_argument("--timeout", type=int, default=600,
                    help="per-request timeout in seconds; first request on a "
                         "cold model includes load time (default 600)")
    ap.add_argument("--system-from-cluster", action="store_true",
                    help="fetch the system prompt from the live lab-coordinator "
                         "Agent instead of the embedded copy")
    args = ap.parse_args()

    system = system_prompt_from_cluster() if args.system_from_cluster else SYSTEM_PROMPT

    all_ok = True
    for model in args.models:
        verdicts = []
        for t in range(args.trials):
            try:
                v = run_trial(args.url, model, system, args.num_ctx, args.timeout)
            except Exception as exc:  # timeout, conn refused, bad json
                v = f"ERROR({exc})"
            verdicts.append(v)
            print(f"{model} trial {t + 1}/{args.trials}: {v}", flush=True)
        ok = sum(1 for v in verdicts if v == "OK")
        print(f"== {model}: {ok}/{args.trials} OK -> "
              f"{'PASS' if ok == args.trials else 'FAIL'} ==\n", flush=True)
        if ok != args.trials:
            all_ok = False

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
