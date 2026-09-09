r"""Turning container labels into domains.

Two label vocabularies are read. Ours:

.. code-block:: yaml

    labels:
      hostbridge.enable: "true"
      hostbridge.domain: "shop.test"
      hostbridge.domains: "shop.test,api.shop.test,*.cdn.shop.test"
      hostbridge.address: "127.0.0.1"

and Traefik's, because a container that already carries ``traefik.http.routers.x.rule``
has said what it answers to, and asking the user to repeat it in a second vocabulary would
be rude. The rule is parsed by :func:`discover.traefikapi.parse_rule`, which is the same
tokeniser used for the API -- a container's labels and the router table must never disagree
about what a rule means.

``hostbridge.enable`` is required for our own labels, and deliberately: turning every
container with a published port into a domain would fill the list with things the user
never asked for. Traefik labels are taken without it, because declaring a ``Host()`` rule
*is* the opt-in.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from core.match import normalise, validate
from core.model import SOURCE_DOCKER, Domain

PREFIX = "hostbridge."

ENABLE_LABEL = f"{PREFIX}enable"
DOMAIN_LABEL = f"{PREFIX}domain"
DOMAINS_LABEL = f"{PREFIX}domains"
ADDRESS_LABEL = f"{PREFIX}address"

TRAEFIK_RULE = re.compile(r"^traefik\.http\.routers\.[^.]+\.rule$")

TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

DEFAULT_ADDRESS = "127.0.0.1"


@dataclass
class LabelResult:
    domains: list[Domain] = field(default_factory=list)
    skipped_invalid: list[str] = field(default_factory=list)


def labels_of(container: Any) -> dict[str, str]:
    labels = container.get("Labels") if isinstance(container, dict) else None
    if not isinstance(labels, dict):
        return {}
    return {str(k).lower(): str(v) for k, v in labels.items()}


def container_name(container: Any) -> str:
    """The friendly name Docker shows, without the leading slash it puts on it."""
    names = container.get("Names") if isinstance(container, dict) else None
    if isinstance(names, list) and names and isinstance(names[0], str):
        return names[0].lstrip("/")
    identifier = container.get("Id") if isinstance(container, dict) else ""
    return str(identifier)[:12]


def wanted_names(labels: dict[str, str]) -> list[str]:
    """Every hostname the labels ask for, ours and Traefik's, in order."""
    found: list[str] = []

    if labels.get(ENABLE_LABEL, "").strip().lower() in TRUE_VALUES:
        single = labels.get(DOMAIN_LABEL, "").strip()
        if single:
            found.append(single)
        for name in labels.get(DOMAINS_LABEL, "").split(","):
            if name.strip():
                found.append(name.strip())

    from discover.traefikapi import parse_rule

    for key, value in labels.items():
        if TRAEFIK_RULE.match(key):
            found.extend(parse_rule(value))

    ordered: list[str] = []
    for name in found:
        if name not in ordered:
            ordered.append(name)
    return ordered


def address_of(labels: dict[str, str]) -> str:
    return labels.get(ADDRESS_LABEL, "").strip() or DEFAULT_ADDRESS


def containers_to_domains(containers: Iterable[Any]) -> LabelResult:
    """Turn a container listing into records ready for the store.

    Owned by the hostname rather than by the container, for the same reason the Traefik
    importer is: a name declared by two containers -- a service and its redirect helper --
    must not vanish when one of them stops and reappear under a different owner on the next
    poll.
    """
    result = LabelResult()
    seen: dict[str, Domain] = {}

    for container in containers or []:
        labels = labels_of(container)
        if not labels:
            continue
        names = wanted_names(labels)
        if not names:
            continue

        owner_name = container_name(container)
        address = address_of(labels)

        for raw in names:
            checked = validate(raw)
            if not checked.ok:
                result.skipped_invalid.append(raw)
                continue

            key = normalise(checked.name)
            existing = seen.get(key)
            if existing is not None:
                if owner_name not in existing.note:
                    existing.note = f"{existing.note}, {owner_name}"
                continue

            domain = Domain(
                name=checked.name,
                address=address,
                source=SOURCE_DOCKER,
                owner=f"host:{key}",
                note=owner_name,
            )
            seen[key] = domain
            result.domains.append(domain)

    return result
