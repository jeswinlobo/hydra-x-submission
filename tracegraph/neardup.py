"""Near-duplicate detection over ingested document bodies.

The track brief names three kinds of noise a real company has: misfiled
documents, **near-duplicates**, and statements that flatly contradict each
other. The third was handled from the start; the second was not handled at all,
and `conflicts.py` said so in a comment rather than doing anything about it —
corroboration discounted only *identical* quote strings.

That gap has a specific cost. Corroboration is one of the four trust signals
that decide which of two contradictory statements wins, and it counts distinct
supporting documents. A policy page copied into an onboarding kit with three
words changed is one source, not two, and counting it twice inflates the version
that happens to have been duplicated. Detecting near-duplicates is therefore not
tidiness — it changes which statement the system trusts.

**MinHash over character shingles**, chosen for what this corpus actually looks
like. Documents here are edited copies: a runbook pasted into a Drive doc, a
Jira description restated in a Slack thread. Token-set overlap alone would call
two unrelated infrastructure documents similar, because they share the same
vocabulary — `latency`, `region`, `rollout` — while differing entirely in what
they say. Shingling preserves local word order, so agreement has to be on
phrasing rather than on topic.

No new dependency: `hashlib` and a fixed set of random permutations are enough,
and a hand-rolled implementation is auditable in a way an opaque library is not.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# Shingle width in words. Five is long enough that two documents sharing a
# shingle have almost certainly shared a sentence rather than a phrase, and
# short enough to survive light editing — a reworded clause breaks the shingles
# that span it and leaves its neighbours intact.
SHINGLE_WORDS = 5

# Permutations in the signature. 128 puts the standard error of the Jaccard
# estimate near 1/sqrt(128) ~ 0.09, which is finer than the threshold needs.
PERMUTATIONS = 128

# Estimated Jaccard above which two documents are treated as near-duplicates.
#
# Measured rather than guessed, and the first guess was wrong. Over the pairs in
# `tests/test_neardup.py`, which are drawn from real corpus shapes:
#
#     runbook vs a two-word-edited copy      0.712
#     runbook with one word changed          0.836
#     two documents sharing only boilerplate 0.222
#     same topic, entirely different content 0.000
#     unrelated text                         0.000
#
# True positives land at 0.71-0.85 and true negatives at 0.00-0.22, so there is
# an empty band between them and the threshold belongs in it. 0.80 was the
# intuitive choice and it sat *above* a genuine copy — an edit breaks every
# shingle spanning it, so a couple of word changes cost far more Jaccard than
# they look like they should. 0.60 keeps a 0.38 margin over the worst false
# positive and 0.11 under the weakest true one.
#
# The asymmetry still matters: a false positive suppresses genuine corroboration
# and makes the system trust a *less* supported version, which is worse than
# missing a duplicate. That argues for the high end of the empty band, not the
# middle of the whole range.
DEFAULT_THRESHOLD = 0.60

_WORD = re.compile(r"[a-z0-9]+")

# The multipliers are masked to 61 bits, so `(a*x + b) & _MASK` is arithmetic
# mod 2^61 rather than mod the Mersenne prime 2^61-1. That is still a valid
# permutation family, but for a different reason than a prime modulus would
# give: `a` is forced odd below, and odd multipliers are invertible mod a power
# of two. Said plainly because an earlier version of this comment claimed the
# prime was doing the work, which would have misled anyone checking the maths.
_MASK = (1 << 61) - 1


def _permutations(count: int = PERMUTATIONS) -> list[tuple[int, int]]:
    """Fixed (a, b) pairs for `h_i(x) = (a*x + b) mod p`.

    Derived from a constant seed rather than `random`, so a signature computed
    today matches one computed next week and stored signatures stay comparable.
    """
    pairs: list[tuple[int, int]] = []
    for i in range(count):
        digest = hashlib.blake2b(str(i).encode(), digest_size=16).digest()
        a = int.from_bytes(digest[:8], "big") | 1  # odd, so it is invertible
        b = int.from_bytes(digest[8:], "big")
        pairs.append((a & _MASK, b & _MASK))
    return pairs


_PERMS = _permutations()


def shingles(text: str, width: int = SHINGLE_WORDS) -> set[int]:
    """Hashed word shingles. Case and punctuation are normalised away."""
    words = _WORD.findall(text.casefold())
    if len(words) < width:
        # A document shorter than one shingle still has to be comparable to
        # itself, so it becomes a single shingle rather than an empty set.
        return {int.from_bytes(
            hashlib.blake2b(" ".join(words).encode(), digest_size=8).digest(),
            "big")} if words else set()
    out: set[int] = set()
    for i in range(len(words) - width + 1):
        window = " ".join(words[i:i + width]).encode()
        out.add(int.from_bytes(
            hashlib.blake2b(window, digest_size=8).digest(), "big"))
    return out


def signature(text: str) -> tuple[int, ...]:
    """A MinHash signature: the minimum of each permutation over the shingles."""
    grams = shingles(text)
    if not grams:
        return ()
    sig = []
    for a, b in _PERMS:
        sig.append(min(((a * g + b) & _MASK) for g in grams))
    return tuple(sig)


def similarity(left: tuple[int, ...], right: tuple[int, ...]) -> float:
    """Estimated Jaccard: the fraction of permutations that agree.

    Two empty signatures are not similar. An empty document resembles nothing,
    including another empty document — treating them as duplicates would merge
    every unparseable body into one.
    """
    if not left or not right or len(left) != len(right):
        return 0.0
    agree = sum(1 for x, y in zip(left, right) if x == y)
    return agree / len(left)


def exact_jaccard(a: str, b: str) -> float:
    """True Jaccard over shingles, for verifying the estimate in tests."""
    sa, sb = shingles(a), shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


@dataclass(frozen=True)
class NearDuplicate:
    left: str
    right: str
    similarity: float


def find_near_duplicates(
    bodies: dict[str, str], *, threshold: float = DEFAULT_THRESHOLD
) -> list[NearDuplicate]:
    """Every pair above `threshold`, as (dsid, dsid, estimated Jaccard).

    Banded LSH would be the scalable form; this is the quadratic one, which is
    honest about its limits — it is run offline over the ingested working set,
    not over 511,962 documents, and it is the ingested set that corroboration
    reads. Bands become worth the complexity when the working set does.
    """
    sigs = {dsid: signature(text) for dsid, text in bodies.items()}
    keys = [k for k in sorted(sigs) if sigs[k]]
    found: list[NearDuplicate] = []
    for i, left in enumerate(keys):
        for right in keys[i + 1:]:
            score = similarity(sigs[left], sigs[right])
            if score >= threshold:
                found.append(NearDuplicate(left, right, round(score, 4)))
    return sorted(found, key=lambda d: -d.similarity)


def canonical_map(
    duplicates: "list[NearDuplicate]",
) -> dict[str, str]:
    """Collapse near-duplicate pairs into one representative per cluster.

    Union-find rather than a `{right: left}` dict, because similarity is **not
    transitive** and the pairs arrive independently. Given `a~b` and `b~c`, a
    naive mapping yields `b->a` and `c->b`, so `a` and `c` canonicalise to
    different representatives and the same cluster is counted twice — which is
    the double-count the caller uses this to avoid. Union-find gives every
    member of a connected component the same representative whether or not each
    pair was directly compared.

    The representative is the lexicographically smallest dsid in the component,
    so the map is stable across runs and independent of pair ordering.
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            # Smallest wins, which is what makes the representative stable.
            lo, hi = (ra, rb) if ra < rb else (rb, ra)
            parent[hi] = lo

    for dup in duplicates:
        union(dup.left, dup.right)

    # Only members of a real cluster are returned. A document with no
    # near-duplicate maps to itself implicitly, and including it would make the
    # map the size of the corpus for no benefit.
    out: dict[str, str] = {}
    for node in parent:
        root = find(node)
        if root != node:
            out[node] = root
    return out
