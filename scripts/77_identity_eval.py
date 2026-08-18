"""Score entity resolution against the quarantined identity oracle.

Track 1's hard part is deciding that `sam`, `@soham` and `S. Ratnaparkhi` are
one person. Everything else in this repo has been measured; that decision never
had been. This script is the measurement, and it is fully deterministic — no
model is called, so the numbers reproduce exactly.

`eval-oracle/employee_directory.yaml` is read **here and nowhere else**. It maps
167 Redwood Inference employees to email, title and manager, which is precisely
the answer `tracegraph/resolve.py` has to derive from documents. Importing it
anywhere upstream would turn the evaluation into a lookup.

The oracle is a *directory*, not a mention-level annotation, and that shapes
every number below. It can say who `Ava Chen` is and it can say that `priya` is
inherently ambiguous because two employees answer to it, but nothing in it says
which Sam a given `sam:` line meant. So a mention is scored only when the oracle
determines its referent, and every mention it does not determine is reported as
out of scope rather than counted as a mistake. The excluded set is large and it
is not a random sample — see the coverage block, which says so out loud.

Read-only: it opens one Bolt session and issues `MATCH … RETURN` only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from itertools import combinations  # noqa: F401  (documented in _pair_counts)
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tracegraph.hydra_client import HydraClient          # noqa: E402
from tracegraph.reconcile import _read_all               # noqa: E402

ORACLE_PATH = REPO / "eval-oracle" / "employee_directory.yaml"
OUT_PATH = REPO / "artifacts" / "identity_eval.json"

MENTION_PAGE = 8000
ENTITY_PAGE = 4000

# The oracle's own mail domain reduces to this root. The corpus spells the same
# employer `redwood.com`, `redwood.ai`, `redwood.inference`, `redwoodinference.com`
# and `redwood.example.com`, so an address is treated as possibly-internal when
# its first meaningful domain label starts with `redwood`. Deliberately loose in
# one direction only: it widens which addresses may be *offered* to the matcher,
# and the matcher still requires a unique name agreement before it accepts one.
_ORG_PREFIX = "redwood"

# Stripped before a directory name is tokenised. `Dr. Aisha Rahman` must match
# the address `aisha_rahman@redwood.ai`, which carries no honorific.
_HONORIFICS = frozenset({"dr", "mr", "mrs", "ms", "prof", "sir"})


# --------------------------------------------------------------------------
# oracle
# --------------------------------------------------------------------------

def load_oracle(path: Path) -> list[dict]:
    """Parse the directory without adding a YAML dependency to the project.

    The file is machine-generated and strictly regular — two levels of nesting,
    every scalar a double-quoted string — and this parser asserts that shape
    rather than assuming it, so a format change fails loudly instead of
    silently scoring against half a directory.
    """
    people: list[dict] = []
    department: str | None = None
    current: dict | None = None
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw == "departments:":
            continue
        if (m := re.match(r'^  ([^ ].*):$', raw)):
            department = m.group(1)
            current = None
            continue
        if (m := re.match(r'^    - (\w+): "(.*)"$', raw)):
            current = {"department": department, m.group(1): m.group(2)}
            people.append(current)
            continue
        if (m := re.match(r'^      (\w+): "(.*)"$', raw)) and current is not None:
            current[m.group(1)] = m.group(2)
            continue
        raise ValueError(f"{path}:{lineno}: unexpected line {raw!r}")
    if not people:
        raise ValueError(f"{path}: parsed no people")
    for person in people:
        if not person.get("name") or not person.get("email"):
            raise ValueError(f"{path}: person without name or email: {person}")
    return people


# --------------------------------------------------------------------------
# identity keys — the matching rule, stated once and used everywhere
# --------------------------------------------------------------------------

def _fold(text: str) -> str:
    """Casefold and strip accents, so `Tomáš Novák` meets `tomas.novak`."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def tokens_of(text: str) -> frozenset[str]:
    """Alphabetic tokens of a name or an email local part, honorifics dropped.

    Single characters are dropped so a middle initial cannot bridge two people.
    This mirrors `tracegraph.parsers.base.name_tokens` but folds accents and
    honorifics, which the resolver does not; the evaluation needs to recognise
    the directory's spelling of a person, not to reproduce the resolver's.
    """
    parts = {t for t in re.split(r"[^a-z]+", _fold(text)) if len(t) > 1}
    return frozenset(parts - _HONORIFICS)


def squash(text: str) -> str:
    """Letters only, order preserved: `Grace O'Connor` and `grace_oconnor`.

    Punctuation is where the corpus and the directory disagree most —
    `O'Brien`/`obrien`, `El-Sayed`/`elsayed`, `Jin Woo Park`/`jinwoo.park` —
    and squashing settles all of them without loosening anything else, because
    order is kept: `chen.ava` does not squash onto `Ava Chen`.
    """
    return re.sub(r"[^a-z]", "", _fold(text))


def local_part(address: str) -> str:
    return address.split("@", 1)[0]


def domain_of(address: str) -> str:
    return address.split("@", 1)[1].casefold() if "@" in address else ""


def org_root(domain: str) -> str:
    parts = [p for p in re.split(r"[.\-]+", (domain or "").casefold()) if len(p) > 1]
    noise = {"com", "net", "org", "io", "ai", "co", "dev", "app", "cloud",
             "inc", "corp", "group", "mail", "email", "www", "us", "uk", "eu"}
    meaningful = [p for p in parts if p not in noise]
    return meaningful[0] if meaningful else ""


def internal_domain(domain: str) -> bool:
    return org_root(domain).startswith(_ORG_PREFIX)


def name_keys(text: str) -> set[tuple[str, object]]:
    """Keys a *name-shaped* string contributes. Two tokens minimum.

    A one-token string — `sam`, `support` — deliberately produces nothing. It
    is the ambiguous case the whole exercise is about, and letting it key a
    directory person would build the answer into the question.
    """
    toks = tokens_of(text)
    if len(toks) < 2:
        return set()
    return {("tokens", toks), ("squash", squash(text))}


def person_keys(person: dict) -> set[tuple[str, object]]:
    address = person["email"].casefold()
    keys: set[tuple[str, object]] = {("email", address)}
    keys |= name_keys(person["name"])
    keys |= name_keys(local_part(address))
    return keys


def entity_emails(entity: dict) -> list[str]:
    return [a.casefold() for a in (entity.get("emails") or "").split(";") if a]


def entity_keys(entity: dict) -> set[tuple[str, object]]:
    """Keys an observed graph identity contributes.

    Addresses are only allowed to key by *name* when they sit on an internal
    domain. Without that guard `priya.sharma@mediloop.com` would key onto the
    Redwood employee Priya Sharma, which is exactly the false merge
    `resolve.py` documents itself resisting — the evaluation must not commit it
    while scoring. An exact address match needs no guard: it is the address.
    """
    keys: set[tuple[str, object]] = set()
    internal = False
    for address in entity_emails(entity):
        keys.add(("email", address))
        if internal_domain(domain_of(address)):
            internal = True
            keys |= name_keys(local_part(address))
    if internal:
        keys |= name_keys(entity.get("name") or "")
    return keys


def match_person(keys: set, index: dict) -> tuple[str | None, set[str]]:
    """Resolve identity keys to at most one directory person.

    Returns `(oid, all_hits)`. Two hits is not a tie to be broken, it is a
    finding: one graph identity carrying two employees is a false merge, so the
    caller records it and the mapping stays empty. Being conservative here
    costs coverage and never invents agreement.
    """
    hits = {index[k] for k in keys if k in index}
    return (next(iter(hits)) if len(hits) == 1 else None), hits


# --------------------------------------------------------------------------
# graph
# --------------------------------------------------------------------------

def read_graph(client: HydraClient, run_id: str | None) -> tuple[str, list[dict], list[dict]]:
    if run_id is None:
        rows = client.bolt_read(
            "MATCH (d:Document) RETURN d.run_id AS r ORDER BY r DESC LIMIT 1")
        if not rows:
            raise SystemExit("no Document carries a run_id; load a slice first")
        run_id = rows[0]["r"]
    entities = _read_all(
        client,
        "MATCH (e:Entity) WHERE e.run_id = $r "
        "RETURN e.id AS id, e.key AS key, e.name AS name, e.emails AS emails, "
        "e.domains AS domains",
        {"r": run_id}, ENTITY_PAGE, "ORDER BY e.id")
    mentions = _read_all(
        client,
        "MATCH (m:Mention) WHERE m.run_id = $r "
        "RETURN m.id AS id, m.dsid AS dsid, m.surface AS surface, "
        "m.normalised AS normalised, m.status AS status, m.method AS method, "
        "m.candidates AS candidates, m.reason AS reason, m.entity AS entity",
        {"r": run_id}, MENTION_PAGE, "ORDER BY m.id")
    return run_id, entities, mentions


_EMAIL_REASON = re.compile(r"^email (\S+)$")


def observed_email(mention: dict) -> str | None:
    """The address the document itself attached to this mention.

    Tier 1 records its evidence as `email <addr>`, and that address came off a
    mail header rather than out of a resolution decision, so using it as ground
    truth is reading the document, not grading the resolver against itself.
    The caveat is real and stated in the report: for tier-1 mentions the label
    and the decision share a source, so their *per-mention* agreement is close
    to definitional. Their pair-level behaviour is not — two addresses
    belonging to two employees landing in one entity is still a false merge,
    and that is what the pairwise and B-cubed numbers are looking at.
    """
    m = _EMAIL_REASON.match(mention.get("reason") or "")
    return m.group(1).casefold() if m else None


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------

def _choose2(n: int) -> int:
    return n * (n - 1) // 2


def pairwise(system: list, gold: list) -> dict:
    """Pairwise P/R/F1 from a contingency table rather than from pairs.

    Enumerating pairs is O(n^2) and n is ten thousand; the cross-tabulation is
    exact and linear. TP counts pairs inside one (system, gold) cell, and the
    two margins give FP and FN.
    """
    assert len(system) == len(gold)
    n = len(system)
    cell = Counter(zip(system, gold))
    sys_size = Counter(system)
    gold_size = Counter(gold)

    tp = sum(_choose2(c) for c in cell.values())
    same_system = sum(_choose2(c) for c in sys_size.values())
    same_gold = sum(_choose2(c) for c in gold_size.values())
    fp = same_system - tp
    fn = same_gold - tp
    tn = _choose2(n) - tp - fp - fn

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"mentions": n, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": precision, "recall": recall, "f1": f1}


def bcubed(system: list, gold: list) -> dict:
    """B-cubed P/R/F1 — averaged per mention, so big clusters cannot dominate."""
    n = len(system)
    if not n:
        return {"mentions": 0, "precision": 0.0, "recall": 0.0, "f1": 0.0}
    cell = Counter(zip(system, gold))
    sys_size = Counter(system)
    gold_size = Counter(gold)
    p = sum(cell[(s, g)] / sys_size[s] for s, g in zip(system, gold)) / n
    r = sum(cell[(s, g)] / gold_size[g] for s, g in zip(system, gold)) / n
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return {"mentions": n, "precision": p, "recall": r, "f1": f1}


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", default=None, help="run_id (default: newest)")
    ap.add_argument("--examples", type=int, default=6,
                    help="how many concrete examples to print per finding")
    args = ap.parse_args()

    if not ORACLE_PATH.exists():
        raise SystemExit(f"oracle not found at {ORACLE_PATH}")
    people = load_oracle(ORACLE_PATH)
    by_oid = {p["email"].casefold(): p for p in people}

    # Directory key index. A key claimed by two employees identifies nobody and
    # is dropped, so the matcher can never pick between two people by accident.
    claims: dict[tuple[str, object], set[str]] = defaultdict(set)
    for person in people:
        for key in person_keys(person):
            claims[key].add(person["email"].casefold())
    index = {k: next(iter(v)) for k, v in claims.items() if len(v) == 1}
    dropped_keys = sorted(k for k, v in claims.items() if len(v) > 1)

    # Which surfaces the directory itself cannot disambiguate: how many
    # employees' names contain a given token. This is what makes "refusing was
    # right" checkable.
    token_owners: dict[str, set[str]] = defaultdict(set)
    for person in people:
        for token in tokens_of(person["name"]):
            token_owners[token].add(person["email"].casefold())

    with HydraClient() as client:
        client.verify()
        run_id, entities, mentions = read_graph(client, args.run)

    entity_by_id = {e["id"]: e for e in entities}

    # ---- entity -> employee -------------------------------------------------
    entity_person: dict[int, str] = {}
    entity_multi: list[dict] = []
    for entity in entities:
        oid, hits = match_person(entity_keys(entity), index)
        if oid:
            entity_person[entity["id"]] = oid
        elif len(hits) > 1:
            entity_multi.append({
                "entity_key": entity["key"], "entity_name": entity["name"],
                "people": sorted(by_oid[h]["name"] for h in hits),
            })

    # ---- mention -> employee ------------------------------------------------
    # Two independent labellers. Where both speak they must agree; a
    # disagreement is dropped rather than adjudicated, because picking one would
    # be the guess this whole exercise exists to avoid.
    gold: dict[int, str] = {}
    gold_source = Counter()
    conflicts: list[dict] = []
    lenient_gold: dict[int, str] = {}

    for mention in mentions:
        surface = mention.get("surface") or ""
        by_email = None
        address = observed_email(mention)
        if address:
            keys = {("email", address)}
            if internal_domain(domain_of(address)):
                keys |= name_keys(local_part(address))
            by_email, _ = match_person(keys, index)
        by_name, _ = match_person(name_keys(surface), index)

        if by_email and by_name and by_email != by_name:
            conflicts.append({"surface": surface,
                              "by_email": by_oid[by_email]["name"],
                              "by_name": by_oid[by_name]["name"]})
            continue
        label = by_email or by_name
        if label:
            gold[mention["id"]] = label
            gold_source["email_anchor" if by_email else "name_anchor"] += 1
            lenient_gold[mention["id"]] = label
            continue
        # Lenient only: a one-token surface that exactly one employee answers
        # to. Assumes the referent is an employee, which the corpus does not
        # guarantee, so it never feeds the headline numbers.
        toks = tokens_of(surface)
        if len(toks) == 1:
            owners = token_owners.get(next(iter(toks)), set())
            if len(owners) == 1:
                lenient_gold[mention["id"]] = next(iter(owners))

    # ---- cluster metrics ----------------------------------------------------
    def cluster_id(mention: dict) -> object:
        # An unresolved mention is its own singleton. That is the standard
        # treatment and it is the honest one: abstention costs recall, and a
        # metric that quietly dropped every refusal would reward refusing.
        if mention.get("status") == "resolved" and mention.get("entity"):
            return ("entity", mention["entity"])
        return ("singleton", mention["id"])

    scored = [m for m in mentions if m["id"] in gold]
    sys_all = [cluster_id(m) for m in scored]
    gold_all = [gold[m["id"]] for m in scored]

    resolved = [m for m in scored if m.get("status") == "resolved" and m.get("entity")]
    sys_res = [cluster_id(m) for m in resolved]
    gold_res = [gold[m["id"]] for m in resolved]

    metrics = {
        "with_abstention_as_singletons": {
            "pairwise": pairwise(sys_all, gold_all),
            "bcubed": bcubed(sys_all, gold_all),
        },
        "resolved_only": {
            "pairwise": pairwise(sys_res, gold_res),
            "bcubed": bcubed(sys_res, gold_res),
        },
    }

    # ---- false merges -------------------------------------------------------
    per_entity: dict[int, list[dict]] = defaultdict(list)
    for mention in resolved:
        per_entity[mention["entity"]].append(mention)

    false_merges = []
    for eid, group in per_entity.items():
        labels = {gold[m["id"]] for m in group}
        if len(labels) < 2:
            continue
        entity = entity_by_id.get(eid, {})
        evidence = []
        for oid in sorted(labels):
            surfaces = sorted({m["surface"] for m in group if gold[m["id"]] == oid})
            evidence.append({"person": by_oid[oid]["name"],
                             "title": by_oid[oid].get("title", ""),
                             "mentions": sum(1 for m in group if gold[m["id"]] == oid),
                             "surfaces": surfaces[:4]})
        false_merges.append({
            "entity_id": eid, "entity_key": entity.get("key"),
            "entity_name": entity.get("name"),
            "people_fused": len(labels),
            "mentions_affected": len(group),
            "detail": sorted(evidence, key=lambda d: -d["mentions"]),
        })
    false_merges.sort(key=lambda d: (-d["people_fused"], -d["mentions_affected"]))

    # ---- split identities ---------------------------------------------------
    per_person: dict[str, list[dict]] = defaultdict(list)
    for mention in resolved:
        per_person[gold[mention["id"]]].append(mention)

    splits = []
    for oid, group in per_person.items():
        eids = {m["entity"] for m in group}
        if len(eids) < 2:
            continue
        frags = []
        for eid in sorted(eids):
            entity = entity_by_id.get(eid, {})
            frags.append({"entity_key": entity.get("key"),
                          "entity_name": entity.get("name"),
                          "mentions": sum(1 for m in group if m["entity"] == eid)})
        splits.append({"person": by_oid[oid]["name"], "email": oid,
                       "fragments": len(eids), "mentions": len(group),
                       "detail": sorted(frags, key=lambda d: -d["mentions"])})
    splits.sort(key=lambda d: (-d["fragments"], -d["mentions"]))

    # Entity-level split, independent of whether any mention was gold-labelled.
    entity_level_split = defaultdict(list)
    for eid, oid in entity_person.items():
        entity_level_split[oid].append(entity_by_id[eid]["key"])
    entity_level_split = {by_oid[o]["name"]: sorted(k)
                          for o, k in entity_level_split.items() if len(k) > 1}

    # ---- precision by method -----------------------------------------------
    # A resolution is decidable when the entity it chose is itself matched to an
    # employee: then "same employee or not" is a fact. When the chosen entity
    # matches no employee the outcome is genuinely unknown — it may be an
    # unlinked fragment of the right person — and is reported separately rather
    # than being scored either way.
    by_method: dict[str, Counter] = defaultdict(Counter)
    method_errors: dict[str, list[dict]] = defaultdict(list)
    for mention in resolved:
        method = mention.get("method") or "unknown"
        chosen = entity_person.get(mention["entity"])
        by_method[method]["scored_mentions"] += 1
        if chosen is None:
            by_method[method]["indeterminate"] += 1
            continue
        if chosen == gold[mention["id"]]:
            by_method[method]["correct"] += 1
        else:
            by_method[method]["wrong"] += 1
            method_errors[method].append({
                "surface": mention["surface"],
                "chose": by_oid[chosen]["name"],
                "gold": by_oid[gold[mention["id"]]]["name"],
                "reason": (mention.get("reason") or "")[:120],
            })

    method_precision = {}
    for method, counts in sorted(by_method.items()):
        decidable = counts["correct"] + counts["wrong"]
        method_precision[method] = {
            "scored_mentions": counts["scored_mentions"],
            "decidable": decidable,
            "correct": counts["correct"],
            "wrong": counts["wrong"],
            "indeterminate": counts["indeterminate"],
            "precision": (counts["correct"] / decidable) if decidable else None,
            "examples_wrong": method_errors[method][:args.examples],
        }

    # Same computation on the lenient labels, which is the only way the graph
    # tier gets a sample at all. Reported apart from the headline for that reason.
    lenient_method: dict[str, Counter] = defaultdict(Counter)
    lenient_errors: dict[str, list[dict]] = defaultdict(list)
    for mention in mentions:
        if mention.get("status") != "resolved" or not mention.get("entity"):
            continue
        label = lenient_gold.get(mention["id"])
        if label is None:
            continue
        method = mention.get("method") or "unknown"
        chosen = entity_person.get(mention["entity"])
        lenient_method[method]["scored_mentions"] += 1
        if chosen is None:
            lenient_method[method]["indeterminate"] += 1
        elif chosen == label:
            lenient_method[method]["correct"] += 1
        else:
            lenient_method[method]["wrong"] += 1
            lenient_errors[method].append({
                "surface": mention["surface"],
                "chose": by_oid[chosen]["name"],
                "gold_assumed": by_oid[label]["name"],
                "reason": (mention.get("reason") or "")[:120],
            })
    lenient_precision = {}
    for method, counts in sorted(lenient_method.items()):
        decidable = counts["correct"] + counts["wrong"]
        lenient_precision[method] = {
            "scored_mentions": counts["scored_mentions"],
            "decidable": decidable,
            "correct": counts["correct"], "wrong": counts["wrong"],
            "indeterminate": counts["indeterminate"],
            "precision": (counts["correct"] / decidable) if decidable else None,
            "examples_wrong": lenient_errors[method][:args.examples],
        }

    # ---- abstention ---------------------------------------------------------
    abstained = [m for m in mentions
                 if m.get("status") == "unresolved" and (m.get("candidates") or 0) > 1]
    abst = Counter()
    abst_examples: dict[str, list[str]] = defaultdict(list)
    for mention in abstained:
        toks = tokens_of(mention.get("surface") or "")
        owners: set[str] = set()
        if toks:
            owners = set.intersection(*(token_owners.get(t, set()) for t in toks)) \
                if all(t in token_owners for t in toks) else set()
        if len(owners) > 1:
            bucket = "ambiguous_in_directory"
        elif len(owners) == 1:
            bucket = "unique_employee_existed"
        else:
            bucket = "no_employee_matches_surface"
        abst[bucket] += 1
        if len(abst_examples[bucket]) < args.examples:
            abst_examples[bucket].append(mention.get("surface") or "")

    correct_abstention = abst["ambiguous_in_directory"] + abst["no_employee_matches_surface"]
    abstention = {
        "unresolved_with_multiple_candidates": len(abstained),
        "buckets": dict(abst),
        "examples": {k: v for k, v in abst_examples.items()},
        "defensible_rate": correct_abstention / len(abstained) if abstained else None,
        "strict_rate_directory_ambiguous_only":
            abst["ambiguous_in_directory"] / len(abstained) if abstained else None,
    }

    # ---- coverage -----------------------------------------------------------
    bot_mentions = sum(1 for m in mentions
                       if (m.get("reason") or "").startswith("automation, not a person"))
    covered_people_by_entity = len(set(entity_person.values()))
    covered_people_by_mention = len(set(gold.values()))
    coverage = {
        "oracle_people": len(people),
        "graph_entities": len(entities),
        "graph_mentions": len(mentions),
        "entities_matched_to_oracle": len(entity_person),
        "entities_matching_two_or_more_people": len(entity_multi),
        "entities_out_of_scope": len(entities) - len(entity_person) - len(entity_multi),
        "mentions_with_strict_gold": len(gold),
        "mentions_with_strict_gold_pct": 100.0 * len(gold) / len(mentions) if mentions else 0.0,
        "mentions_excluded_no_oracle_referent": len(mentions) - len(gold) - len(conflicts),
        "mentions_excluded_label_conflict": len(conflicts),
        "mentions_with_lenient_gold": len(lenient_gold),
        "gold_label_sources": dict(gold_source),
        "oracle_people_seen_via_entities": covered_people_by_entity,
        "oracle_people_seen_via_scored_mentions": covered_people_by_mention,
        "bot_mentions_excluded_by_system": bot_mentions,
        "directory_keys_dropped_as_ambiguous": [list(k) if isinstance(k[1], str)
                                                else [k[0], sorted(k[1])]
                                                for k in dropped_keys],
    }

    result = {
        "run_id": run_id,
        "oracle": str(ORACLE_PATH.relative_to(REPO)),
        "matching_rule": {
            "entity_to_employee": (
                "An Entity matches an employee when their identity keys "
                "intersect on exactly one employee. Keys are (a) an exact email "
                "address, (b) the token set of a name or email local part with "
                "two or more tokens, accents and honorifics folded, and (c) the "
                "letters-only squash of the same, which settles O'Connor/oconnor "
                "and El-Sayed/elsayed. Name-shaped keys are taken from an "
                "Entity's addresses only when the address sits on a domain whose "
                "root starts with 'redwood', so priya.sharma@mediloop.com cannot "
                "be read as the Redwood employee of the same name. A key claimed "
                "by two employees is dropped from the index; an Entity hitting "
                "two employees is recorded as a false merge and mapped to neither."
            ),
            "mention_to_employee": (
                "Strict gold uses two labellers that must agree: the address the "
                "document attached to the mention (tier-1 evidence, read off a "
                "mail header) and the mention surface when it carries two or more "
                "name tokens. A mention with neither is excluded as out of scope, "
                "not counted wrong. Lenient gold additionally labels a one-token "
                "surface that exactly one employee answers to; it assumes the "
                "referent is an employee, which the corpus does not guarantee, so "
                "it is reported separately and never in the headline numbers."
            ),
            "clusters": (
                "System cluster = the Entity a Mention resolved to. An unresolved "
                "mention is a singleton, so abstention costs recall rather than "
                "being silently excluded. Gold cluster = the employee."
            ),
        },
        "coverage": coverage,
        "metrics": metrics,
        "false_merges": {
            "count": len(false_merges),
            "mentions_affected": sum(f["mentions_affected"] for f in false_merges),
            "entities_matching_two_or_more_employees": entity_multi,
            "detail": false_merges,
        },
        "split_identities": {
            "count": len(splits),
            "people_with_scored_mentions": len(per_person),
            "detail": splits,
            "entity_level_splits": entity_level_split,
        },
        "precision_by_method": method_precision,
        "precision_by_method_lenient": lenient_precision,
        "abstention": abstention,
        "label_conflicts": conflicts[:args.examples],
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, sort_keys=False), encoding="utf-8")

    report(result, args.examples)
    return 0


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:5.1f}%"


def report(r: dict, examples: int) -> None:
    c, m = r["coverage"], r["metrics"]
    print(f"\n=== identity evaluation — run {r['run_id']} ===")
    print(f"oracle: {r['oracle']} (read here only)\n")

    print("-- coverage ------------------------------------------------------")
    print(f"  mentions in graph            {c['graph_mentions']:>6}")
    print(f"  ... with oracle ground truth {c['mentions_with_strict_gold']:>6}"
          f"  ({c['mentions_with_strict_gold_pct']:.1f}%)"
          f"   [email-anchored {c['gold_label_sources'].get('email_anchor', 0)},"
          f" name-anchored {c['gold_label_sources'].get('name_anchor', 0)}]")
    print(f"  ... excluded, oracle silent  {c['mentions_excluded_no_oracle_referent']:>6}"
          f"   (of which {c['bot_mentions_excluded_by_system']} are bots the system already skips)")
    print(f"  ... excluded, labels clashed {c['mentions_excluded_label_conflict']:>6}")
    print(f"  entities in graph            {c['graph_entities']:>6}"
          f"   matched to an employee: {c['entities_matched_to_oracle']}"
          f"   out of scope: {c['entities_out_of_scope']}")
    print(f"  employees in directory       {c['oracle_people']:>6}"
          f"   seen via entities: {c['oracle_people_seen_via_entities']}"
          f"   via scored mentions: {c['oracle_people_seen_via_scored_mentions']}")

    for label, key in (("abstention counted (unresolved = singleton)", "with_abstention_as_singletons"),
                       ("resolved mentions only", "resolved_only")):
        pw, b3 = m[key]["pairwise"], m[key]["bcubed"]
        print(f"\n-- {label} --")
        print(f"  pairwise   P {_pct(pw['precision'])}  R {_pct(pw['recall'])}"
              f"  F1 {_pct(pw['f1'])}   (TP {pw['tp']}, FP {pw['fp']}, FN {pw['fn']}, TN {pw['tn']})")
        print(f"  B-cubed    P {_pct(b3['precision'])}  R {_pct(b3['recall'])}"
              f"  F1 {_pct(b3['f1'])}   over {b3['mentions']} mentions")

    fm = r["false_merges"]
    print(f"\n-- false merges: {fm['count']} entities fuse two or more employees"
          f" ({fm['mentions_affected']} mentions) --")
    for item in fm["detail"][:examples]:
        who = " + ".join(f"{d['person']} x{d['mentions']}" for d in item["detail"])
        print(f"  {item['entity_key']}  [{item['entity_name']}]")
        print(f"      {who}")
        for d in item["detail"]:
            print(f"        {d['person']:<24} surfaces {d['surfaces']}")
    if fm["entities_matching_two_or_more_employees"]:
        print("  entities whose own identity keys hit two employees:")
        for item in fm["entities_matching_two_or_more_employees"][:examples]:
            print(f"      {item['entity_key']} -> {item['people']}")

    sp = r["split_identities"]
    print(f"\n-- split identities: {sp['count']} of {sp['people_with_scored_mentions']}"
          f" employees spread across several entities --")
    for item in sp["detail"][:examples]:
        print(f"  {item['person']:<22} {item['fragments']} entities, {item['mentions']} mentions")
        for d in item["detail"][:5]:
            print(f"        {d['entity_key']:<48} x{d['mentions']}")

    print("\n-- precision by resolution method (strict gold) --")
    print(f"  {'method':<22}{'scored':>8}{'decidable':>11}{'correct':>9}{'wrong':>7}"
          f"{'indet':>7}{'precision':>11}")
    for method, s in r["precision_by_method"].items():
        print(f"  {method:<22}{s['scored_mentions']:>8}{s['decidable']:>11}"
              f"{s['correct']:>9}{s['wrong']:>7}{s['indeterminate']:>7}"
              f"{_pct(s['precision']):>11}")
    for method, s in r["precision_by_method"].items():
        for e in s["examples_wrong"][:2]:
            print(f"      wrong [{method}] {e['surface']!r} -> {e['chose']}"
                  f" (gold {e['gold']})")

    print("\n-- precision by method (lenient gold: one-token surfaces with a "
          "single employee match; assumes the referent is an employee) --")
    print(f"  {'method':<22}{'scored':>8}{'decidable':>11}{'correct':>9}{'wrong':>7}"
          f"{'indet':>7}{'precision':>11}")
    for method, s in r["precision_by_method_lenient"].items():
        print(f"  {method:<22}{s['scored_mentions']:>8}{s['decidable']:>11}"
              f"{s['correct']:>9}{s['wrong']:>7}{s['indeterminate']:>7}"
              f"{_pct(s['precision']):>11}")
    for method, s in r["precision_by_method_lenient"].items():
        for e in s["examples_wrong"][:2]:
            print(f"      wrong [{method}] {e['surface']!r} -> {e['chose']}"
                  f" (assumed {e['gold_assumed']})")

    a = r["abstention"]
    print(f"\n-- abstention: {a['unresolved_with_multiple_candidates']} mentions left "
          "unresolved while holding several candidates --")
    for bucket, n in sorted(a["buckets"].items(), key=lambda kv: -kv[1]):
        print(f"  {bucket:<32}{n:>6}   e.g. {a['examples'].get(bucket, [])[:4]}")
    print(f"  refusal defensible (directory ambiguous, or names no employee): "
          f"{_pct(a['defensible_rate'])}")
    print(f"  refusal forced by directory ambiguity alone:                    "
          f"{_pct(a['strict_rate_directory_ambiguous_only'])}")

    if r["label_conflicts"]:
        print("\n-- mentions dropped because the two labellers disagreed --")
        for e in r["label_conflicts"]:
            print(f"  {e['surface']!r}: email says {e['by_email']}, name says {e['by_name']}")

    print(f"\nwrote {OUT_PATH.relative_to(REPO)}")


if __name__ == "__main__":
    raise SystemExit(main())
