"""Name normalisation, validation and the one match ladder the whole app shares.

Every surface needs these rules: the resolver to answer a query, the console screen and the
GUI dialog to tell the user a name is unusable, and the NRPT planner to decide which
namespaces to register. Keeping them here means the three can never disagree about what
``*.foo.test`` covers -- a disagreement that would surface as "it resolves but the dialog
says it is invalid", which is horrible to debug.

The ladder, in order: **exact FQDN -> wildcard with the longest suffix winning -> nothing**,
and "nothing" is what makes the resolver forward the query upstream.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

WILDCARD_PREFIX = "*."

MAX_NAME_LENGTH = 253
MAX_LABEL_LENGTH = 63

#: Reserved for multicast DNS by RFC 6762. Windows and macOS resolve these through
#: mDNS/LLMNR before any DNS server is consulted, so our resolver is never asked.
MDNS_SUFFIX = "local"

#: Real gTLDs that Chrome ships on the HSTS preload list, so ``http://x.dev`` is upgraded
#: to HTTPS before a request ever leaves the browser. Not exhaustive -- the real list runs
#: to hundreds of entries -- but these are the ones a developer actually reaches for.
HSTS_PRELOADED_TLDS = frozenset(
    {
        "app",
        "bank",
        "boo",
        "channel",
        "dad",
        "day",
        "dev",
        "esq",
        "foo",
        "gle",
        "google",
        "hangout",
        "how",
        "ing",
        "insurance",
        "meme",
        "mov",
        "new",
        "nexus",
        "page",
        "phd",
        "prof",
        "rsvp",
        "search",
        "soy",
        "zip",
    }
)

#: Reserved by RFC 2606 and RFC 6761 for exactly this purpose: safe to shadow, never
#: resolvable on the public internet, and on no preload list.
SAFE_TLDS = ("test", "localhost", "example", "invalid")

_LABEL_ALLOWED = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_")


@dataclass(frozen=True)
class Issue:
    """A message for the user, as a catalogue key plus its parameters.

    ``core`` stays free of presentation: the console screen and the Qt dialog each call
    ``t(issue.key, **issue.params)`` themselves.
    """

    key: str
    params: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Validation:
    name: str
    """The normalised name. Usable even when there are warnings."""

    error: Issue | None = None
    warnings: tuple[Issue, ...] = ()

    @property
    def ok(self) -> bool:
        return self.error is None


def _encode_label(label: str) -> str:
    """IDNA-encode one label, leaving pure-ASCII labels untouched.

    Label by label rather than the whole name through ``str.encode("idna")``: that codec
    refuses a name containing an empty label or a wildcard, and we would rather validate
    those ourselves and say which part is wrong.
    """
    if label.isascii():
        return label
    try:
        return label.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        # Left as-is; validate() rejects it below with a message naming the label.
        return label


def normalise(raw: str) -> str:
    """Case-fold, drop the trailing root dot, and punycode any non-ASCII labels."""
    name = (raw or "").strip().lower().rstrip(".")
    if not name:
        return ""

    wildcard = name.startswith(WILDCARD_PREFIX)
    if wildcard:
        name = name[len(WILDCARD_PREFIX) :]

    encoded = ".".join(_encode_label(label) for label in name.split("."))
    return f"{WILDCARD_PREFIX}{encoded}" if wildcard else encoded


def is_wildcard(name: str) -> bool:
    return name.startswith(WILDCARD_PREFIX)


def apex_of(name: str) -> str:
    """The bare name a wildcard is built on: ``*.foo.test`` becomes ``foo.test``."""
    return name[len(WILDCARD_PREFIX) :] if is_wildcard(name) else name


def tld_of(name: str) -> str:
    return apex_of(name).rsplit(".", 1)[-1]


def validate(raw: str) -> Validation:
    """Check a name a human typed and collect everything worth telling them.

    Warnings never block. Shadowing a real production domain is the whole use case here, so
    this refuses only names that cannot work at all.
    """
    name = normalise(raw)
    if not name:
        return Validation(name="", error=Issue("domain.error_empty"))

    if name == "*":
        # Checked before the position rule below, which would otherwise report a bare star
        # as being in the wrong place. It is not misplaced -- it is the whole name, and it
        # would route every query on the machine into HostBridge.
        return Validation(name=name, error=Issue("domain.error_wildcard_bare"))

    body = apex_of(name)
    if "*" in body:
        return Validation(name=name, error=Issue("domain.error_wildcard_position"))

    if len(name) > MAX_NAME_LENGTH:
        return Validation(
            name=name,
            error=Issue("domain.error_too_long", {"limit": str(MAX_NAME_LENGTH)}),
        )

    labels = body.split(".")
    for label in labels:
        if not label:
            return Validation(name=name, error=Issue("domain.error_empty_label"))
        if len(label) > MAX_LABEL_LENGTH:
            return Validation(
                name=name,
                error=Issue(
                    "domain.error_label_too_long",
                    {"label": label, "limit": str(MAX_LABEL_LENGTH)},
                ),
            )
        if label.startswith("-") or label.endswith("-"):
            return Validation(
                name=name, error=Issue("domain.error_label_hyphen", {"label": label})
            )
        bad = sorted(set(label) - _LABEL_ALLOWED)
        if bad:
            return Validation(
                name=name,
                error=Issue(
                    "domain.error_label_chars", {"label": label, "chars": " ".join(bad)}
                ),
            )

    if len(labels) < 2:
        # A single label never reaches a DNS server as typed: Windows appends the
        # connection-specific suffix and the browser treats it as a search term.
        return Validation(name=name, error=Issue("domain.error_single_label"))

    warnings: list[Issue] = []
    tld = labels[-1]

    if tld == MDNS_SUFFIX:
        warnings.append(Issue("domain.warn_mdns", {"suggested": f"{labels[-2]}.test"}))
    elif tld in HSTS_PRELOADED_TLDS:
        warnings.append(Issue("domain.warn_hsts_tld", {"tld": tld}))

    if is_wildcard(name):
        warnings.append(Issue("domain.warn_wildcard_apex", {"apex": body}))

    return Validation(name=name, error=None, warnings=tuple(warnings))


@dataclass(frozen=True)
class Match:
    """What the zone found for a query."""

    address: str

    apex: str
    """The name to put in a synthesised SOA. For an exact record that is the name itself;
    for a wildcard it is the suffix the wildcard is built on, because a NODATA answer has
    to be authoritative for something that actually exists in the zone."""

    wildcard: bool


def parent_suffixes(name: str) -> Iterator[str]:
    """Yield the ancestors of ``name``, longest first.

    ``a.b.foo.test`` yields ``b.foo.test``, ``foo.test``, ``test``. The name itself is never
    yielded, and that is exactly why ``*.foo.test`` does not match ``foo.test`` -- the rule
    wildcards genuinely have in DNS, and the one people forget.
    """
    labels = name.split(".")
    for index in range(1, len(labels)):
        yield ".".join(labels[index:])


class Zone:
    """A compiled, read-only view of the enabled records.

    Rebuilt whole on every store change rather than mutated. A zone is a few hundred entries
    at most and building one costs microseconds, so swapping an immutable object into place
    is cheaper than the locking an in-place update would need -- and an in-flight query can
    never observe a half-updated table.
    """

    __slots__ = ("_exact", "_serial", "_wildcards")

    def __init__(
        self, exact: dict[str, str], wildcards: dict[str, str], serial: int = 0
    ) -> None:
        self._exact = exact
        self._wildcards = wildcards
        self._serial = serial

    @classmethod
    def from_domains(cls, domains: Iterable, serial: int = 0) -> Zone:
        """Compile the enabled records. Disabled ones are simply absent, so they forward."""
        exact: dict[str, str] = {}
        wildcards: dict[str, str] = {}
        for domain in domains:
            if not domain.enabled:
                continue
            name = normalise(domain.name)
            if not name:
                continue
            if is_wildcard(name):
                wildcards[apex_of(name)] = domain.address
            else:
                exact[name] = domain.address
        return cls(exact, wildcards, serial)

    @property
    def serial(self) -> int:
        return self._serial

    def __len__(self) -> int:
        return len(self._exact) + len(self._wildcards)

    def match(self, qname: str) -> Match | None:
        """Resolve ``qname`` against the zone, or ``None`` to forward upstream."""
        name = normalise(qname)
        if not name:
            return None

        found = self._exact.get(name)
        if found is not None:
            return Match(address=found, apex=name, wildcard=False)

        # Longest suffix wins, and parent_suffixes yields longest first, so the first hit
        # is the answer. A dict lookup per label beats maintaining a trie: a name has ten
        # labels at the very worst, and the trie would be one more thing to keep correct.
        for suffix in parent_suffixes(name):
            found = self._wildcards.get(suffix)
            if found is not None:
                return Match(address=found, apex=suffix, wildcard=True)
        return None

    def lookup(self, qname: str) -> str | None:
        """The address for ``qname``, or ``None``. Convenience over :meth:`match`."""
        found = self.match(qname)
        return found.address if found is not None else None

    def namespaces(self) -> tuple[str, ...]:
        """The namespaces to register with the system resolver, deduplicated and sorted.

        An exact name is registered as itself; a wildcard as a leading-dot suffix, which is
        how both NRPT and systemd-resolved routing domains spell "this subtree".
        """
        found = set(self._exact)
        found.update(f".{suffix}" for suffix in self._wildcards)
        return tuple(sorted(found))
