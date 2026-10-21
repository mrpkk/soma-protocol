# Build your own hub in 30 minutes

Soma is a protocol, not a service: the value is that two independent implementations
can trust each other's envelopes. So the shortest path to "we speak Soma" is:

1. implement the seven operations,
2. pass `tools/check_conformance.py` against your hub,
3. get a number you can show a partner.

Nothing below needs our code — only the wire format in `PROTOCOL.md` and the schema in
`schemas/envelope.schema.json`.

## The seven operations

| Operation | Input | Must return |
|---|---|---|
| `send` | envelope body | `{ok, id, signature}` — the envelope is signed and stored |
| `inbox` | agent name | envelopes waiting for that agent, each with `context` packed to the budget |
| `claim` | id, agent, optional payment | the envelope, exclusively; a refusal keeps it available |
| `complete` | id, result, agent | closes the envelope; repeated completion is refused |
| `route` | id | `{agent, reason, cost_per_call, rejected}` — the decision explains itself |
| `verify` | envelope JSON | `{ok, reason, algorithm}` |
| `stats` | — | counts by status and by tier |

Names, quirks and error codes are fixed; transport is not. Ours is HTTP (stdlib) plus an
MCP adapter — yours can be gRPC, NATS or a file drop, as long as the envelope survives.

## The envelope, honestly

```json
{
  "id": "6db013cfa3c54b29",
  "sender": "research",
  "to": "verifier",
  "goal": "check the claim",
  "task": "re-verify the numbers in section 3",
  "capability": ["verify"],
  "token_budget": 500,
  "context_query": "",
  "cold_refs": [],
  "mandate": {"max_cost_usd": 0.005, "allowed_payees": ["verifier"]},
  "price_usdc": 0.005,
  "cost_usdc": 0.005,
  "ttl_s": 3600,
  "visibility": "shared",
  "created_at": 1792563175.2547185,
  "status": "queued",
  "signature": {"algorithm": "ed25519", "signature": "…", "public_key": "…", "signer": "hub"}
}
```

Four properties carry the trust, and each one is a test:

1. **The signature covers creation fields only.** `status`, `routing`, `result` and any
   assignment change after signing and are excluded; everything else is covered.
2. **Canonical JSON normalizes numbers.** `3600` and `3600.0` must produce the same bytes —
   otherwise a JS sender and a Python receiver never agree. This is the single easiest
   place to break interoperability, and it is checked.
3. **The budget is hard.** Context is packed to `token_budget`; what does not fit is
   *declared* in `dropped`, never silently cut.
4. **A refusal keeps the envelope.** Signature, payment and mandate are checked before the
   claim is taken; a legitimate worker can still pick it up afterwards.

## Run the suite

```bash
pip install mcp
python3 tools/check_conformance.py https://your-hub.example/mcp
```

Output is a pass/fail line per property (C0–C9). Exit code is 0 only when everything passes.
Against our reference demo node:

```
[PASS] C0 seven tools present
[PASS] C1 envelope accepted and signed — id=e85ad0d28d904004 signature=ed25519
[PASS] C2 signature verifies
[PASS] C3 context fits the budget — 0 of 500
[PASS] C4 degradation declared — dropped=0
[PASS] C5 router explains its decision — agent=verifier
[PASS] C6 one claimer — envelope unavailable (status=claimed)
[PASS] C7 result closes the envelope
[PASS] C8 priced work needs a receipt
[PASS] C9 state is readable
10/10 checks passed
```

For a Python implementation you can also run it in-process:

```bash
MITRA_CONFORMANCE_IMPL="my_package.hub:MyHubFactory" pytest tests/test_conformance.py -v
```

The factory receives a temp directory and returns an object with the seven methods above.

## Public demo node

`https://rta.lat/soma/mcp` — streamable HTTP, no token, 30 requests an hour per client.
It is a sandbox, and it says so:

- separate database, separate signing key, no access to any real registry;
- **every envelope is stored as `visibility: public`** — the node overrides what the sender
  asks for, so nothing private can land there;
- envelopes expire by `ttl_s` like anywhere else;
- quota is per IP (behind the tunnel, `X-Forwarded-For` wins).

Use it to try the wire format, not to keep your work.

## What "passing" does not prove

The suite proves the mechanics, not the judgement: a hub can pass while its router picks
bad agents, and a receipt can be well-formed while the money never moved. Those are
commercial problems, and we say so instead of hiding it behind a green check.

## MCP client config

```json
{
  "mcpServers": {
    "soma": {
      "type": "http",
      "url": "https://rta.lat/soma/mcp"
    }
  }
}
```
