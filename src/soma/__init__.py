"""Soma — протокол коммуникации агентов: доверие, память, расчёт.

Одна мысль: сообщение между агентами — это **конверт**, а не текст в чате.
В конверте: цель, задача, бюджет токенов, мандат, цена, подпись, срок.
Всё остальное (память, транспорт, деньги, история) — перевозчики этого письма.
"""

from .envelope import (Envelope, Mandate, canonical, check_mandate, digest,  # noqa: F401
                        route, sign, validate, verify)
from .memory import Memory  # noqa: F401
from .registry import Registry  # noqa: F401
from .server import Hub, serve  # noqa: F401

__version__ = "1.3.0"
PROTOCOL = "soma/1"
