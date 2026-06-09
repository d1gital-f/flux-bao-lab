# AI model tests

Tests for the kagent + Ollama agent stack. Born out of a debugging session
(2026-06-09) where coordinator→reader delegation silently hung forever.

## Background: why these tests exist

kagent exposes sub-agents to the model as tools with generated names like
`kagent__NS__lab_k8s_reader`. Small models frequently mangle that name
(e.g. dropping an underscore: `kagent__NS_lab_k8s_reader`) or emit the call
as plain text instead of a structured tool call. Ollama then either returns
the broken call as text or — worse — returns a **completely empty message**.
The kagent ADK runner receives nothing, closes silently, and the A2A task
stays in `working` forever. No error is logged anywhere.

Measured 2026-06-09 (ollama 0.30.7, real coordinator system prompt, 5 trials):

| Model      | Structured tool call OK | Failure mode                |
|------------|-------------------------|-----------------------------|
| qwen2.5:3b | 0/5                     | empty response              |
| llama3.2   | 2/5                     | tool call printed as text   |
| qwen3:4b   | not yet benchmarked     | (first run timed out on cold CPU load) |

Lesson: a model that passes with a bare prompt can still fail with the real
~500-token system prompt. Always benchmark with the production prompt.

## bench_tool_calls.py — model-level benchmark

Hits Ollama's `/api/chat` directly with the coordinator's system prompt and
the exact kagent tool name, then classifies each response:
`OK` / `TEXT` / `EMPTY` / `WRONGNAME`.

```zsh
kubectl port-forward -n kagent svc/ollama 21434:11434 &
./bench_tool_calls.py --models llama3.2 qwen3:4b --trials 5
```

Notes:
- First request on a cold model includes load time; the default
  `--timeout 600` allows for slow CPU-only loads. Run one warm-up request
  (or accept a slow first trial).
- `--system-from-cluster` pulls the live coordinator prompt via kubectl so
  the bench can't drift from the deployed agent.
- A model is delegation-safe only at (or very near) 100% OK.

## test_delegation.py — end-to-end A2A test

Sends a real delegation request to `lab-coordinator` over A2A JSON-RPC and
verifies the round-trip: task completes, the history contains an actual
`lab_k8s_reader` turn, and no tool-call-as-text artifacts.

```zsh
kubectl port-forward -n kagent svc/lab-coordinator 18080:8080 &
./test_delegation.py
```

Failure modes it detects:
- **stuck in `working`** → model returned an empty/unparseable tool call
  (the qwen2.5:3b failure mode);
- **tool call as text artifact** → model printed the call instead of
  invoking it (the llama3.2 failure mode);
- **no reader turn in history** → coordinator answered without delegating.

## Suggested workflow when changing the model

1. Pull the candidate model: `kubectl -n kagent exec deploy/ollama -- ollama pull <model>`
2. `./bench_tool_calls.py --models <model>` — must PASS
3. Update `apps/kagent-modelconfig.yaml` (+ the postStart pull in
   `apps/ollama.yaml`), commit, push, `flux reconcile kustomization apps`
4. `./test_delegation.py` — must PASS
5. Remove losing models to keep node disk below the kubelet disk-pressure
   threshold: `kubectl -n kagent exec deploy/ollama -- ollama rm <model>`
