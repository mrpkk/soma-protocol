# Soma

**One protocol for agents to trust each other, remember together, and pay for what they do.**

A message between agents is an **envelope**, not a chat line. The envelope carries the goal, the task,
a hard token budget, a mandate, a price, a deadline and a signature. Memory, transport, money and
history are just carriers of that envelope.

```
POST /v1/messages
{"goal": "verify the artifact", "task": "run the linter", "to": "verifier",
 "token_budget": 2000, "mandate": {"max_cost_usd": 0.5}, "price_usdc": 0.005}
```

- **Budget travels with the message.** The receiver knows the limit before it starts.
- **Refusals come with a reason.** Every decline names the rule that fired.
- **Degradation is declared.** If context did not fit, the report says what was dropped.
- **Signatures verify offline.** Ed25519, public key included — no service, no secret.
- **Cost is measured, not listed.** Tokens, CPU, bytes, writes, external calls.

## 60 seconds

```bash
pip install -e ".[dev]"
soma keygen                       # ключ подписи Ed25519
soma serve                        # хаб на http://127.0.0.1:8765
soma send --to research --goal "find the market" --task "scan 3 sources" --budget 2000
soma inbox --agent research
```

Or in Python:

```python
from mitra import Envelope, Hub

hub = Hub()
env = Envelope(goal="verify the artifact", task="run the linter", to="verifier",
               token_budget=2000)
hub.send(env)                       # подписан и поставлен в очередь
hub.inbox("verifier")               # что ждёт агента
hub.claim(env.id, "verifier")       # забрать (атомарно)
hub.complete(env.id, {"text": "ok"})
```

## Why

Agents already work. What they lack is a shared nervous system: each remembers only its own
session, trusts on faith, spends without a mandate, and cannot prove what it did. Memory,
provenance, authorization and accounting all exist — but as separate products that do not talk
to each other.

Soma is the glue. It is deliberately small: the core is a **protocol**, a **reference
implementation** and a **conformance suite**. Not a platform, not a framework, not a vendor.

## What is in the box

| Piece | What it is |
|---|---|
| `src/mitra/envelope.py` | the protocol core: envelope, canonicalization, signing, mandate rules, router, JSON Schema |
| `src/mitra/memory.py` | three memory tiers — hot queue, warm facts (SQLite + FTS5), cold artifacts by hash |
| `src/mitra/server.py` | HTTP transport (stdlib only) — `/v1/messages`, `/v1/inbox`, `/v1/claim`, `/v1/complete`, `/v1/route` |
| `src/mitra/mcp_adapter.py` | MCP adapter — the same operations as MCP tools (`soma_send`, `soma_inbox`, `soma_claim`, `soma_complete`, `soma_route`, `soma_verify`, `soma_stats`) |
| `src/mitra/payments.py` | usage metering (tokens/CPU/bytes/writes/calls) and the x402-compatible payment contract |
| `src/mitra/anchors.py` | append-only history chain (hash-linked), with a seam for Arweave/IPFS/Nostr |
| `src/mitra/registry.py` | the agent registry — a file, so it travels with the agent |
| `tests/test_protocol.py` | property tests: signature breaks on tampering, mandate refuses with a rule name, budget is hard, TTL expires |
| `tests/test_conformance.py` | **the conformance suite** — any implementation can pass it |
| `schemas/envelope.schema.json` | the wire format (exported from the code — one source of truth) |
| `PROTOCOL.md` | the specification |

## Build your own hub, prove it speaks Mitra

The conformance suite is the point of this project. Point it at your implementation:

```bash
MITRA_CONFORMANCE_IMPL="my_package.hub:MyHubFactory" pytest tests/test_conformance.py -v
```

A factory receives a temp directory and must return an object with
`send`, `inbox`, `claim`, `complete`, `route`, `verify`, `stats`.
Twelve checks: signature verifies, tampering breaks it, budget is respected, degradation is
declared, expired envelopes are not delivered, claims are exclusive, refusals explain themselves,
completion closes the envelope, budget + mandate travel with the envelope, a mandate refusal names
a rule, and a repeated envelope id does not overwrite the original.

## Composing with what already exists

Soma does not replace any of these — it carries envelopes between them:

- **MCP** — an adapter exposes the same operations as MCP tools.
- **A2A** — `agents.json` in this repo is an A2A Agent Card; envelopes map onto A2A tasks.
- **x402** — the payment header contract is defined in `src/mitra/payments.py`.
- **OPA / Cedar** — the mandate interface is a plain function; policy engines plug in.
- **Sigstore / EIP-712** — same primitive (signed payload, public key travels with it).
- **Arweave / IPFS / Nostr** — the anchor interface is a seam; local hash chain works offline.

## What is open and what is not

This is the whole point of the split, so it is written down plainly.

| Layer | Example | License | Why |
|---|---|---|---|
| **Protocol** | envelope, three memory tiers, router, the five seams, the conformance suite | **MIT, forever** | a protocol only wins if others implement it; ours is a candidate, not a law |
| **Engines** | deep artifact verification, mandate engine with ledger, true cost metering, the rule base of a skill linter | **closed** | this is what we sell |
| **Service** | hosted hub, guarantees, x402 settlement, reputation, shared history, registry | **closed** | recurring revenue, and reputation cannot be copied — only accumulated |

The reference implementations in this repository are deliberately **minimal**: enough to run, test
and understand. They are not the engines. Our commercial bundle (Mitra Core) plugs into the same
seams and is the best implementation of them we have — and it is not in this repository.

> If you are reading this to decide whether to build on Soma: yes, build on it. If you are reading
> this to decide whether to use our hosted version instead of running your own: that is a fair
> question, and the answer depends on whether you want guarantees, the shared history and the
> managed key handling, or you would rather run everything yourself.

## Plugging your own engine

The five seams are protocols (`soma/interfaces.py`): `Signer`, `MandateEvaluator`, `MemoryTier`,
`Meter`, `Anchor`. A plugin is any object that satisfies them.

```python
# my_engine.py
from soma.envelope import Mandate

class StrictMandate:
    name = "my-engine"

    def check(self, action, mandate, only=None):
        if action.get("payee") == "mallory":
            return False, "payee: blocked by my engine"
        return True, "ok"

def components():
    return {"mandate": StrictMandate()}
```

Load it two ways:

```bash
# 1) environment — for experiments and local runs
SOMA_PLUGINS="my_engine:components" soma serve

# 2) entry point — for a distributed package
#    [project.entry-points."soma.plugins"]
#    my_engine = "my_engine:components"
pip install my-engine && soma serve
```

`GET /healthz` reports what is actually connected:

```json
{"components": {"signer": "ed25519", "mandate": "my-engine", "meter": "usage",
                "memory": "Memory", "anchor": "LocalChain", "plugins": ["my-engine"]}}
```

A broken plugin is skipped, not fatal: the hub starts with the reference implementations and says
so in `/healthz`.

## Server profile

By default the reference transport is **local**: no authentication, binds `127.0.0.1` — a
component to put behind your own gateway. For a public deployment there is a managed profile:

```bash
SOMA_MANAGED=1 SOMA_TOKEN=<secret> SOMA_QUOTA_PER_HOUR=100 soma serve
```

| Variable | Meaning |
|---|---|
| `SOMA_MANAGED=1` | require `Authorization: Bearer $SOMA_TOKEN` on every request |
| `SOMA_TOKEN` | the token; if it is not set, the managed profile denies **everything** |
| `SOMA_QUOTA_PER_HOUR` | max envelopes per sender per hour (0 = no limit) |
| `SOMA_MAX_BODY` | max request body in bytes (default 1 MiB) |

Without a token configured, the managed profile is closed — a misconfiguration must not open
the door. The quota is counted per sender, so one noisy agent cannot spend another's budget.

## Trust, stated plainly

- The reference HTTP transport has **no authentication** and binds `127.0.0.1`. Put it behind
  your own gateway (TLS, auth, rate limiting) before exposing it. Bodies are capped at 1 MiB.
- A signature proves **integrity** by itself. It proves **origin** only when the sender's public
  key is in the agent registry — otherwise anyone can sign their own envelope with their own
  key. The reference implementation checks the registry when it has one.
- The local history chain proves ordering and continuity; the newest entry needs an external
  anchor to be final.

## Status

Beta. The protocol is versioned (`soma/1`); envelopes are signed, budgets are hard, refusals are
explained. See `ROADMAP.md` for what is next and `SECURITY.md` for the trust model.

## License

MIT.
