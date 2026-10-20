"""Mitra — командная строка. 60 секунд до первого конверта."""

from __future__ import annotations

import argparse
import json
import sys

from .envelope import Envelope, Mandate
from .memory import Memory
from .registry import Registry
from .server import Hub, serve


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mitra", description="Mitra — протокол между агентами")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send", help="отправить конверт")
    s.add_argument("--to", default="*")
    s.add_argument("--from", dest="sender", default="cli")
    s.add_argument("--goal", required=True)
    s.add_argument("--task", required=True)
    s.add_argument("--capability", default="")
    s.add_argument("--query", default="")
    s.add_argument("--budget", type=int, default=2000)
    s.add_argument("--max-cost", type=float, default=None)
    s.add_argument("--price-usdc", type=float, default=None)
    s.add_argument("--ttl", type=float, default=3600)

    sub.add_parser("serve", help="поднять HTTP-хаб")
    k = sub.add_parser("keygen", help="создать ключ подписи Ed25519")
    k.add_argument("--path", default=None)
    sub.add_parser("stats", help="состояние")
    sub.add_parser("schema", help="вывести JSON-схему конверта")
    sub.add_parser("agents", help="реестр исполнителей")

    i = sub.add_parser("inbox")
    i.add_argument("--agent", required=True)
    i.add_argument("--limit", type=int, default=10)

    c = sub.add_parser("claim")
    c.add_argument("--id", required=True)
    c.add_argument("--agent", required=True)

    d = sub.add_parser("complete")
    d.add_argument("--id", required=True)
    d.add_argument("--result", required=True)
    d.add_argument("--agent", default="")

    r = sub.add_parser("route")
    r.add_argument("--id", required=True)

    args = ap.parse_args(argv)
    hub = Hub(Memory(), Registry())

    if args.cmd == "send":
        env = Envelope(goal=args.goal, task=args.task, to=args.to, sender=args.sender,
                       capability=[x for x in args.capability.split(",") if x],
                       context_query=args.query, token_budget=args.budget,
                       price_usdc=args.price_usdc, ttl_s=args.ttl,
                       mandate=Mandate(max_cost_usd=args.max_cost))
        print(json.dumps(hub.send(env), ensure_ascii=False))
    elif args.cmd == "serve":
        serve()
    elif args.cmd == "keygen":
        from .envelope import generate_key
        print(json.dumps(generate_key(args.path), ensure_ascii=False, indent=2))
    elif args.cmd == "schema":
        from .envelope import _ENVELOPE_SCHEMA
        print(json.dumps(_ENVELOPE_SCHEMA, ensure_ascii=False, indent=2))
    elif args.cmd == "stats":
        print(json.dumps(hub.memory.stats(), ensure_ascii=False, indent=2))
    elif args.cmd == "agents":
        print(json.dumps(hub.registry.agents, ensure_ascii=False, indent=2))
    elif args.cmd == "inbox":
        print(json.dumps(hub.inbox(args.agent, args.limit), ensure_ascii=False, indent=2, default=str))
    elif args.cmd == "claim":
        print(json.dumps(hub.claim(args.id, args.agent), ensure_ascii=False, indent=2, default=str))
    elif args.cmd == "complete":
        print(json.dumps(hub.complete(args.id, {"text": args.result}, args.agent or None),
                         ensure_ascii=False))
    elif args.cmd == "route":
        print(json.dumps(hub.route(args.id), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
