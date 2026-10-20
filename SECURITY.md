# Trust model

## What is signed

The **envelope payload** — goal, task, recipient, sender, capability, token budget, context
query, cold references, mandate, price ceiling, TTL, id and creation time. The signature does
**not** cover `status`, `routing` and `result`: those are written after the envelope is created,
by the hub and by the executor.

The canonical form is `json.dumps(payload, sort_keys=True, ensure_ascii=False,
separators=(",", ":"))`. Any change to a signed field invalidates the signature.

## What the signature buys

Proof of **integrity** always, and proof of **origin** only when the sender's key is known
independently.

A valid signature means: this envelope was created by the holder of that key and has not been
altered since. It does not mean the task is sensible, that the sender is honest, or that the
artifact it describes is good.

**Origin requires an out-of-band key.** The public key travels inside the envelope, so on its
own it proves nothing about *who* sent it — anyone can sign with their own key and claim
another sender. The registry is that out-of-band channel: when an agent's `public_key` is
registered, verification compares the envelope's key with the registry and refuses a mismatch.
Without a registry, treat the signature as integrity only.

Verifying needs the public key that travels with the signature — no service call, no secret.
That is the reason Ed25519 is the default: a skeptic can check without trusting the hub.

## Trust boundaries

| Boundary | What crosses it | Protection |
|---|---|---|
| sender → hub | envelope | signature; size and schema limits |
| hub → executor | envelope + assembled context | budget; context is budgeted, cold refs are hashes |
| executor → third party | result | the executor's own mandate; the hub does not execute actions |
| hub → history | events | hash-linked append-only chain (финальность записи — только внешним якорем) |

## What the hub will not do

- It does not execute the task. It routes, remembers, accounts, and refuses.
- It does not evaluate the truth of a result. `attest`-style verification of content is a
  separate layer that signs the artifact, not the envelope.
- It does not keep a secret for a signed envelope: verification is public-key only.
- It does not silently drop context. Degradation is always reported.

## Deployment notes

- Put the HTTP transport behind a reverse proxy with TLS and authentication. The reference
  implementation binds `127.0.0.1` by default and has **no authentication** — it is meant to
  sit behind your own gateway. Bodies are capped at 1 MiB; add rate limiting yourself.
- Keep the signing key out of the repository and out of images. `MITRA_SIGNING_KEY` points to
  the key file; the file is created with mode 0600.
- Rate-limit inbound envelopes at the gateway. The protocol has no built-in spam control; a
  public deployment must add its own (token, quota, or proof-of-work).
- Register agent public keys in the registry if you need origin to be provable, and keep the
  registry write-protected.
- Memory is local. If a deployment federates nodes, encrypt the warm tier at rest and keep the
  private key separate from the data.

## Reporting

Open an issue with a reproduction. For security-sensitive reports, contact the maintainers
directly first.
