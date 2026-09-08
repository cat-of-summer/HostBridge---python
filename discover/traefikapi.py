r"""Importing router hostnames from a running Traefik.

This is the direct replacement for ``hosts.bat``, which polled the same endpoint, pulled
``Host(`…`)`` out of each rule with a regular expression, and wrote the results into the
Windows hosts file between marker comments.

The regular expression is the part worth not copying. ``Host\((.*)\)`` is greedy, so on

    Host(`a.test`) && PathPrefix(`/api`)

it captures ``` `a.test`) && PathPrefix(`/api` ``` and yields a "hostname" containing a path.
Rules with two predicates are the common case, not an edge case, so this module tokenises
instead: find a predicate by name, scan forward to *its* closing parenthesis while tracking
backtick and quote state, and split the arguments.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from core import log
from core.match import normalise, validate
from core.model import SOURCE_TRAEFIK, Domain

#: Predicates that name a host. ``HostSNI`` belongs to TCP routers and carries the same kind
#: of value; ``HostHeader`` is the Traefik v1 spelling and still turns up in old configs.
HOST_PREDICATES = ("HostSNI", "HostHeader", "Host")

#: Matched but skipped: a regular expression is not a name we can register, and guessing a
#: concrete hostname from one would invent domains the user never asked for.
REGEXP_PREDICATES = ("HostRegexp", "HostSNIRegexp")

_PREDICATE = re.compile(
    r"\b(" + "|".join((*HOST_PREDICATES, *REGEXP_PREDICATES)) + r")\s*\(",
)

_QUOTES = "`'\""


@dataclass
class ImportResult:
    domains: list[Domain] = field(default_factory=list)
    skipped_regexp: list[str] = field(default_factory=list)
    skipped_invalid: list[str] = field(default_factory=list)


def _scan_arguments(rule: str, start: int) -> tuple[str, int]:
    """Return the text inside a predicate's parentheses and the index just past them.

    Tracks quoting, so a ``)`` or ``,`` inside a backticked value cannot end the scan early.
    """
    depth = 1
    quote = ""
    index = start
    while index < len(rule):
        char = rule[index]
        if quote:
            if char == quote:
                quote = ""
        elif char in _QUOTES:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return rule[start:index], index + 1
        index += 1
    # Unbalanced. Take what there is rather than discarding the whole rule: a router with a
    # truncated rule still names a host we can usefully register.
    return rule[start:], len(rule)


def _split_arguments(raw: str) -> list[str]:
    """Split a predicate's argument list on commas that are not inside quotes."""
    values: list[str] = []
    current: list[str] = []
    quote = ""
    for char in raw:
        if quote:
            if char == quote:
                quote = ""
            else:
                current.append(char)
            continue
        if char in _QUOTES:
            quote = char
            continue
        if char == ",":
            values.append("".join(current))
            current = []
            continue
        current.append(char)
    values.append("".join(current))
    return [value.strip() for value in values if value.strip()]


def parse_rule(rule: str) -> list[str]:
    """Every hostname a Traefik rule matches on, in order and deduplicated.

    Regular-expression predicates are recognised so they can be skipped deliberately rather
    than parsed into nonsense.
    """
    if not rule:
        return []

    found: list[str] = []
    index = 0
    while True:
        match = _PREDICATE.search(rule, index)
        if match is None:
            break
        arguments, index = _scan_arguments(rule, match.end())
        if match.group(1) in REGEXP_PREDICATES:
            continue
        for value in _split_arguments(arguments):
            if value not in found:
                found.append(value)
    return found


def regexp_predicates(rule: str) -> list[str]:
    """The regexp host predicates in a rule, for reporting what was skipped."""
    found: list[str] = []
    index = 0
    while True:
        match = _PREDICATE.search(rule, index)
        if match is None:
            return found
        arguments, index = _scan_arguments(rule, match.end())
        if match.group(1) in REGEXP_PREDICATES:
            found.extend(_split_arguments(arguments))


def routers_to_domains(routers: Iterable[Any], address: str = "127.0.0.1") -> ImportResult:
    """Turn the API's router list into records ready for the store.

    ``owner`` is the router name, which is what lets a later sync tell "this router is gone"
    from "the user deleted the domain" -- see the reconciliation rule in
    :meth:`core.store.DomainStore.reconcile`.
    """
    result = ImportResult()
    seen: dict[str, Domain] = {}

    for entry in routers or []:
        if not isinstance(entry, dict):
            continue
        status = entry.get("status")
        if isinstance(status, str) and status.lower() not in ("enabled", ""):
            # A router Traefik itself has disabled or failed to build would resolve to a
            # backend that is not there.
            continue

        rule = entry.get("rule")
        if not isinstance(rule, str):
            continue
        owner = entry.get("name")
        if not isinstance(owner, str) or not owner:
            continue

        result.skipped_regexp.extend(regexp_predicates(rule))

        for raw_name in parse_rule(rule):
            checked = validate(raw_name)
            if not checked.ok:
                result.skipped_invalid.append(raw_name)
                continue
            key = normalise(checked.name)
            existing = seen.get(key)
            if existing is not None:
                # The same hostname routinely arrives from more than one router: a real
                # Traefik carries both `php-apacheeabr` and `php-apache-redirecteabr` for
                # eabr.org. Record the extra router in the note and keep one domain.
                if owner not in existing.note:
                    existing.note = f"{existing.note}, {owner}" if existing.note else owner
                continue

            domain = Domain(
                name=checked.name,
                address=address,
                source=SOURCE_TRAEFIK,
                # Owned by the hostname, not by the router that happened to mention it
                # first. If it were the router, taking down the redirect router would
                # delete a domain the other router still serves, and the next poll would
                # add it back under a different owner -- churn on every deploy.
                owner=f"host:{key}",
                note=owner,
            )
            seen[key] = domain
            result.domains.append(domain)

    return result


async def fetch_routers(api_url: str, *, timeout: float = 5.0) -> list[Any]:
    """GET ``/api/http/routers`` from a running Traefik."""
    from discover.httpjson import HttpError, get_json

    url = api_url.rstrip("/") + "/api/http/routers"
    try:
        payload = await get_json(url, timeout=timeout)
    except HttpError as exc:
        raise TraefikUnavailable(str(exc)) from exc
    if not isinstance(payload, list):
        raise TraefikUnavailable("the router endpoint did not return a list")
    return payload


class TraefikUnavailable(Exception):
    """Traefik is not answering. Never fatal: the resolver runs perfectly well without it."""


async def import_domains(api_url: str, address: str = "127.0.0.1", *, timeout: float = 5.0):
    routers = await fetch_routers(api_url, timeout=timeout)
    result = routers_to_domains(routers, address)
    if result.skipped_regexp:
        log.write(f"traefik: skipped {len(result.skipped_regexp)} regexp host rules")
    if result.skipped_invalid:
        log.warn(f"traefik: skipped unusable names {result.skipped_invalid}")
    return result
