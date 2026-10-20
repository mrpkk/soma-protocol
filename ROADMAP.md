# Roadmap

Protocol: `soma/1`. Everything below is additive unless it says otherwise.

## Done

- Envelope v1: fields, canonicalization, JSON Schema (exported from code).
- Signing: Ed25519 with the public key inside the envelope; HMAC fallback; tamper detection.
- Mandate: four rules in fixed order, denial by default, refusals that name the rule.
- Router: capability ∩ mandate ∩ price; cheapest wins; refusal lists every rejected agent.
- Memory: hot queue, warm facts (SQLite + FTS5 trigram, versions, edges), cold artifacts by hash;
  hard budget with a declared `dropped` list.
- HTTP transport (standard library only) and a CLI (`send`, `inbox`, `claim`, `complete`, `route`,
  `stats`, `keygen`, `schema`).
- Money: usage metering and the x402-compatible payment contract.
- History: hash-linked append-only chain with a seam for external anchors.
- Conformance suite: 10 checks any implementation can pass.
- `agents.json` (A2A Agent Card) for discovery.
- **MCP adapter** (`src/mitra/mcp_adapter.py`): seven tools over the same envelopes; stdio and HTTP.

## Next

- **A2A adapter** — envelopes mapped onto A2A tasks; card publishing.
- **x402 settlement** — a reference receiver that verifies a payment receipt and settles it.
- **External anchors** — Arweave and IPFS publishers behind the `Anchor` interface.
- **Policy adapter** — OPA / Cedar as pluggable mandate evaluators.
- **Token calibration** — replace the character-per-token estimate with a real tokenizer.

## Deliberately not planned

- Agent runtime, orchestration framework, workflow engine. Mitra carries messages; it does not
  decide what an agent does next.
- A hosted control plane. The hub is a small process; the protocol is the product.
- Mandatory blockchain. History is hash-chained locally; external anchoring is an adapter, not a
  requirement.
