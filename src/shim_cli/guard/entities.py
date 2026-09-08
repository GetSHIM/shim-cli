from __future__ import annotations

from collections.abc import Iterable

ENTITY_TYPES = (
    "EMAIL",
    "PHONE",
    "CREDIT_CARD",
    "IBAN",
    "IP_ADDRESS",
    "MAC_ADDRESS",
    "US_SSN",
    "TR_NATIONAL_ID",
    "TR_VKN",
    "SECRET",
    "DB_URI",
)
DEFAULT_ENTITIES = ENTITY_TYPES


def normalize_entities(entities: Iterable[object]) -> tuple[str, ...]:
    values = tuple(entities)
    if any(not isinstance(value, str) for value in values):
        raise ValueError("entity names must be strings")
    selected = set(values)
    if len(values) != len(selected):
        raise ValueError("entity names must not be repeated")
    unknown = selected.difference(ENTITY_TYPES)
    if unknown:
        raise ValueError("unsupported entity name")
    return tuple(entity for entity in ENTITY_TYPES if entity in selected)
