# Mitra protocol — `soma/1`

Status: **beta**. Version of the wire format is `soma/1`; the field set below is frozen for it.
JSON Schema: [`schemas/envelope.schema.json`](schemas/envelope.schema.json) (exported from the code —
the code is the single source of truth).

## 1. The envelope

A message between agents is an **envelope**. It is the only thing agents exchange; memory,
transport, money and history are carriers.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `id` | string | yes | unique message id (16 hex chars) |
| `created_at` | number | yes | unix seconds, sender clock |
| `ttl_s` | number | yes | lifetime in seconds; expired envelopes are never delivered |
| `goal` | string | yes | *why* — readable without context |
| `task` | string | yes | *what* — the work to be done |
| `to` | string | no | recipient agent id, or `*` for broadcast |
| `sender` | string | no | who sent it |
| `capability[]` | string[] | no | capabilities required — the router's filter |
| `token_budget` | integer | no | hard budget for the context that will be assembled |
| `context_query` | string | no | what to retrieve from warm memory |
| `cold_refs[]` | string[] | no | `sha256:` references to cold artifacts (hashes, not bodies) |
| `mandate` | object | no | what the sender permits — checked before acting |
| `price_usdc` | number | no | maximum the sender will pay for this message |
| `cost_usdc` | number | no | what the work costs; a receipt for this amount is required to claim it |
| `status` | string | no | `queued` / `claimed` / `done` / `expired` / `declined` |
| `signature` | object | no | signature over the canonical envelope |
| `routing` | object | no | who was chosen and why |
| `result` | object | no | the answer |

Absent means *not specified*; `null` is never sent.

### 1.1 Canonicalization and signing

The signed payload is the envelope **minus** `signature`, `routing`, `result` and `status` —
those change after the envelope is created. Canonical form is
`json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))`.

```
signature = {
  "algorithm": "ed25519",              # preferred: public key travels with the signature
  "signature": "<base64>",
  "public_key": "<base64>"
}
```

Ed25519 is preferred because verification needs **no service and no secret** — the public key is
in the envelope. If Ed25519 is unavailable, `hmac-sha256` is used and verification requires the
shared secret. An unsigned envelope is reported as `algorithm: "none"` and must be treated as
unverified.

**Rule:** any change to a signed field invalidates the signature. That is the point.

### 1.2 Mandate

```
"mandate": {
  "max_cost_usd": 0.5,
  "max_calls": 10,
  "allowed_payees": ["alice"],
  "expires_at": 1760000000,
  "rules": ["amount", "payee", "calls", "validity_window"]
}
```

Rules are evaluated in a **fixed order**, so a refusal names the most important reason rather
than an incidental one:

| # | rule | fires when |
|---|---|---|
| 1 | `validity_window` | the action happens after `expires_at` |
| 2 | `amount` | the amount exceeds `max_cost_usd` |
| 3 | `payee` | the payee is not in `allowed_payees` |
| 4 | `calls` | the batch exceeds `max_calls` |

A refusal is a string naming the rule: `amount: сумма 50.0 выше мандата 0.5`.

**Denial is the default.** Three cases are refusals, not permissions:

- an **unknown rule name** (`rules: ["typo"]`) → `unknown_rule: [...]`; a typo must not
  silently disable every check;
- an **enabled rule without its field** → the field is missing, so the check cannot pass;
- a **non-numeric value** → refused, not treated as zero.

A rule can be checked separately, because the rules concern different moments:
`payee`, `calls` and `validity_window` govern **delivery** of the work, `amount` governs
**payment**. A delivery is not blocked by a missing amount; a payment is not blocked by an
unrelated payee rule.

The mandate interface is a plain function, so an external policy engine (OPA, Cedar) can be
substituted without changing the protocol.

### 1.3 Routing

The router picks an executor from the registry:

1. capability intersection — the agent must offer at least one required capability;
2. mandate filter — `cost_per_call` must fit `max_cost_usd`;
3. price ceiling — `cost_per_call` must fit `price_usdc`;
4. of the survivors, the cheapest wins; ties break by name.

4. the mandate's `allowed_payees` must contain the recipient (broadcast `*` is not checked
   against the payee list — the recipient is decided by whoever claims it).

If nobody survives, the answer is a refusal **with the reason per rejected agent** — a task is
never dropped silently.

## 2. Memory: three tiers, hard budget

| Tier | Where | Rule |
|---|---|---|
| hot | envelope queue (SQLite) | goal and task are **always** in context — the one deliberate budget overrun; TTL cleans up |
| warm | facts with versions and edges (SQLite + FTS5, trigram) | retrieved by query, selected under budget |
| cold | artifacts addressed by `sha256` | fetched by hash, included only if it fits |

Assembling context returns four things, and the fourth is not optional:

```json
{"text": "...", "tokens": 118, "budget": 2000,
 "sources": ["hot", "warm", "cold"],
 "dropped": ["warm: факт 42 не влез в бюджет", "cold:sha256:ab…: не найден"]}
```

**The context never grows; the store does.** That is the whole anti-degradation mechanism: the
model is told what it does not have instead of being silently truncated.

## 3. Transport

The protocol is transport-agnostic. One envelope, many carriers.

### 3.1 HTTP (reference implementation, standard library only)

```
GET  /healthz                 liveness + stats
GET  /v1/agents               registry
POST /v1/messages             send an envelope
GET  /v1/inbox?agent=…        envelopes waiting for an agent
POST /v1/claim                claim an envelope (exclusive)
POST /v1/complete             close an envelope with a result
POST /v1/route                choose an executor
GET  /v1/envelopes/{id}       envelope + signature verdict + history
```

The reference transport has **no authentication** and binds `127.0.0.1`. It is a component to
put behind your own gateway; a public deployment must add authentication, a body-size limit
(the reference caps bodies at 1 MiB) and rate limiting.

### 3.2 MCP

An adapter exposes the same operations as MCP tools (`send`, `inbox`, `claim`, `complete`,
`route`, `verify`, `stats`). MCP carries envelopes; it does not change their shape.

### 3.3 A2A

An A2A task is an envelope with `to` and `task`; the Agent Card format (`agents.json`) is used
for discovery. A2A is the transport; the mandate and the signature are ours.

### 3.4 x402 and payment

Two numbers, two directions:

- `price_usdc` — the **ceiling** the sender is willing to pay;
- `cost_usdc` — the **price of the work**, set by the receiver.

If `cost_usdc` is set, the envelope may only be claimed with a payment receipt. The receipt
travels in the `X-PAYMENT` header (or in the `payment` field of the claim request):

```json
{"amount": 0.005, "asset": "USDC", "payee": "0x…", "network": "eip155:8453",
 "signature": "…"}
```

The protocol checks that the amount covers `cost_usdc`, that the asset is USDC, and that the
payee matches when it is given. Without a receipt the claim is refused with
`{"code": "payment_required", "amount_usdc": 0.005}` — and the envelope stays in the queue, so
it can be claimed after payment. Signature verification and chain settlement belong to the
transport: the protocol fixes the contract, not the network.

### 3.5 A2A

A2A is a carrier, not a second protocol. The mapping is explicit:

| Envelope | A2A task |
|---|---|
| `id` | `taskId`, `contextId` |
| `goal` + `task` | the first history message (text part) |
| budget, mandate, signature | `metadata.soma` (the whole envelope) |
| result | a completed task with an artifact, or an agent message |

`soma.a2a` does the translation in both directions, and `task_to_envelope` refuses a task
without `metadata.soma` — a foreign task is not silently treated as ours.
**The protocol fixes the contract, not the chain.** Settlement is a transport concern; a
deployment chooses Base, Solana or another rail.

## 4. Money: measured, not listed

`Usage` meters what actually happened — `tokens`, `cpu_seconds`, `bytes_out`, `storage_writes`,
`external_calls` — and turns it into a cost. There is no price list for "an agent call":
a call that burns 30k tokens and 4 seconds of CPU is priced by that, not by a tier table.

The reference rates live in `src/mitra/payments.py` and are configuration, not the market.

## 5. History

Every envelope leaves a trace: created, routed, claimed, completed. Records are hash-linked
(`prev` + payload → `hash`), so the chain is append-only and verifiable offline — and a tampered
middle record breaks the link to the next one. The anchor interface is deliberately small so
that Arweave, IPFS or Nostr can take over publication while the local chain keeps working
offline.

**Honest limit:** the last record of a local chain can always be recomputed, so the chain alone
does not prove that the newest entry was never altered — it needs an external anchor (Arweave,
IPFS, a notary) for that. Until then the chain proves ordering and continuity, not finality.

## 6. Errors and refusals

| Case | Response |
|---|---|
| invalid envelope | `{"ok": false, "error": "invalid_envelope", "details": [...]}` |
| cannot sign | `{"ok": false, "error": "cannot_sign", "details": ["нечем подписать: ..."]}` |
| duplicate id | `{"ok": false, "error": "duplicate_id", "details": [...]}` |
| bad body | `{"error": "bad_request", "details": [...]}` (400) |
| payment required | `{"ok": false, "code": "payment_required", "amount_usdc": 0.005}` |
| no executor | `{"agent": null, "reason": "...", "rejected": [...]}` |
| mandate violated | `{"ok": false, "reason": "<rule>: <why>"}` |
| already claimed | `{"ok": false, "reason": "конверт недоступен (status=claimed)"}` |
| expired | not delivered; `status` becomes `expired` |
| signature invalid | reported as unverified; the payload is **not** silently accepted |

## 7. Conformance

An implementation conforms when it passes `tests/test_conformance.py`:

- C1 send returns an envelope id;
- C2 envelopes are signed and verify;
- C3 tampering invalidates the signature;
- C4 the assembled context respects `token_budget`;
- C5 degradation is declared (`dropped` present);
- C6 expired envelopes are not delivered;
- C7 claims are exclusive;
- C8 routing refusals explain themselves;
- C9 completion closes the envelope;
- C10 budget and mandate travel with the envelope;
- C11 a mandate refusal names a rule;
- C12 a repeated envelope id does not overwrite the original;
- C13 work with a price is not delivered without a payment receipt.

Routing never touches a signed field: the chosen executor is stored separately
(`assigned_to`), so the signature stays valid after `route`.

Point the suite at your implementation:

```bash
MITRA_CONFORMANCE_IMPL="my_package.hub:MyHubFactory" pytest tests/test_conformance.py -v
```

## 8. Versioning

`soma/1` is a protocol identifier, not a marketing version. Breaking changes to the envelope
get a new identifier (`mitra/2`). Additive fields keep the identifier and must be ignored by
old readers — which is why the schema forbids `null` values: absence means "not specified",
so a new field never breaks an old parser.
