"""Score entity resolution against the quarantined identity oracle.

Track 1's hard part is deciding that `sam`, `@soham` and `S. Ratnaparkhi` are
one person. Everything else in this repo has been measured; that decision never
had been. This is the measurement, and it is fully deterministic — no model is
called, so the numbers reproduce exactly.

`eval-oracle/employee_directory.yaml` is read **here and nowhere else**. It maps
167 Redwood Inference employees to email, title and manager, which is precisely
the answer `tracegraph/resolve.py` has to derive from documents; importing it
upstream would turn resolution into a lookup.

The oracle is a *directory*, not a mention-level annotation, and that shapes
every number below. It can say who `Ava Chen` is, and it can say that `priya` is
inherently ambiguous because two employees answer to it, but nothing in it says
which Sam a given `sam:` line meant. So a mention is scored only where the
directory determines its referent, and everything else is reported as out of
scope rather than counted as a mistake.

Two label sets are produced, and the difference between them is the report:

* **strict** — the referent is pinned by an internal email address on the
  mention, or by a full-name surface on a mention whose address is internal.
  Sound, and small.
* **lenient** — strict, plus one-token surfaces that exactly one employee
  answers to. That assumes the referent is an employee, which this corpus does
  not guarantee, so it never carries a headline number. It exists because it is
  the only label set that reaches the graph-evidence tier at all.

Read-only: one Bolt session, `MATCH … RETURN` only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tracegraph.hydra_client import HydraClient          # noqa: E402
from tracegraph.reconcile import _read_all               # noqa: E402

ORACLE_PATH = REPO / "eval-oracle" / "employee_directory.yaml"
OUT_PATH = REPO / "artifacts" / "identity_eval.json"

MENTION_PAGE = 8000
ENTITY_PAGE = 4000

# The directory's own mail domain reduces to this root. The corpus spells the
# same employer `redwood.com`, `redwood.ai`, `redwood.inference`,
# `redwoodinference.com` and `redwood.example.com`, so an address counts as
# possibly-internal when its first meaningful domain label starts with
# `redwood`. Loose in one direction only: it widens which addresses may be
# offered to the matcher, which still requires unique name agreement to accept.
_ORG_PREFIX = "redwood"

# Stripped before a directory name is tokenised: `Dr. Aisha Rahman` has to meet
# the address `aisha_rahman@redwood.ai`, which carries no honorific.
_HONORIFICS = frozenset({"dr", "mr", "mrs", "ms", "prof", "sir"})


# --------------------------------------------------------------------------
# oracle
# --------------------------------------------------------------------------

def load_oracle(path: Path) -> list[dict]:
    """Parse the directory without adding a YAML dependency to the project.

    The file is machine-generated and strictly regular — two levels of nesting,
    every scalar double-quoted — and this parser asserts that shape rather than
    assuming it, so a format change fails loudly instead of silently scoring
    against half a directory.
    """
    people: list[dict] = []
    department: str | None = None
    current: dict | None = None
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw == "departments:":
            continue
        if (m := re.match(r'^  ([^ ].*):$', raw)):
            department, current = m.group(1), None
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
    """Alphabetic tokens of a name or email local part, honorifics dropped.

    Single characters are dropped so a middle initial cannot bridge two people.
    This mirrors `tracegraph.parsers.base.name_tokens` but also folds accents
    and honorifics: the evaluation needs to recognise the directory's spelling
    of a person, not to reproduce the resolver's tokeniser.
    """
    return frozenset({t for t in re.split(r"[^a-z]+", _fold(text))
                      if len(t) > 1} - _HONORIFICS)


def squash(text: str) -> str:
    """Letters only, order preserved: `Grace O'Connor` meets `grace_oconnor`.

    Punctuation is where corpus and directory disagree most — `O'Brien`/`obrien`,
    `El-Sayed`/`elsayed`, `Jin Woo Park`/`jinwoo.park` — and squashing settles
    all of them without loosening anything else, because order is kept:
    `chen.ava` does not squash onto `Ava Chen`.
    """
    return re.sub(r"[^a-z]", "", _fold(text))


def local_part(address: str) -> str:
    return address.split("@", 1)[0]


def domain_of(address: str) -> str:
    return address.split("@", 1)[1].casefold() if "@" in address else ""


def org_root(domain: str) -> str:
    noise = {"com", "net", "org", "io", "ai", "co", "dev", "app", "cloud",
             "inc", "corp", "group", "mail", "email", "www", "us", "uk", "eu"}
    parts = [p for p in re.split(r"[.\-]+", (domain or "").casefold()) if len(p) > 1]
    meaningful = [p for p in parts if p not in noise]
    return meaningful[0] if meaningful else ""


def internal_domain(domain: str) -> bool:
    return org_root(domain).startswith(_ORG_PREFIX)


def name_keys(text: str) -> set[tuple[str, object]]:
    """Keys a *name-shaped* string contributes. Two tokens minimum.

    A one-token string — `sam`, `support` — deliberately produces nothing. It is
    the ambiguous case the whole exercise is about, and letting it key a
    directory person would build the answer into the question.
    """
    toks = tokens_of(text)
    if len(toks) < 2:
        return set()
    return {("tokens", toks), ("squash", squash(text))}


def person_keys(person: dict) -> set[tuple[str, object]]:
    address = person["email"].casefold()
    return ({("email", address)} | name_keys(person["name"])
            | name_keys(local_part(address)))


def entity_emails(entity: dict) -> list[str]:
    return [a.casefold() for a in (entity.get("emails") or "").split(";") if a]


def entity_keys(entity: dict) -> set[tuple[str, object]]:
    """Keys an observed graph identity contributes.

    Addresses may key by *name* only on an internal domain. Without that guard
    `priya.sharma@mediloop.com` keys onto the Redwood employee of the same name
    — exactly the false merge `resolve.py` documents itself resisting, and the
    evaluation must not commit it while scoring for it. An exact address match
    needs no guard: it is the address.
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
    """Resolve identity keys to at most one employee.

    Two hits is not a tie to be broken, it is a finding: one graph identity
    carrying two employees is a false merge, so the caller records it and the
    mapping stays empty. Conservative here costs coverage and never invents
    agreement.
    """
    hits = {index[k] for k in keys if k in index}
    return (next(iter(hits)) if len(hits) == 1 else None), hits


# --------------------------------------------------------------------------
# graph
# --------------------------------------------------------------------------

def read_graph(client: HydraClient, run_id: str | None):
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
    truth reads the document rather than grading the resolver against itself.
    The caveat is real and named in the report: for tier-1 mentions the label
    and the decision share a source, so their *per-mention* agreement is close
    to definitional. Their pair-level behaviour is not — two addresses belonging
    to two employees landing in one entity would still be a false merge.
    """
    m = _EMAIL_REASON.match(mention.get("reason") or "")
    return m.group(1).casefold() if m else None


def cluster_id(mention: dict) -> object:
    """The system's cluster for a mention; an abstention is a singleton.

    That is the standard treatment and the honest one: refusing costs recall,
    where a metric that quietly dropped every refusal would reward refusing.
    """
    if mention.get("status") == "resolved" and mention.get("entity"):
        return ("entity", mention["entity"])
    return ("singleton", mention["id"])


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------

def _choose2(n: int) -> int:
    return n * (n - 1) // 2


def pairwise(system: list, gold: list) -> dict:
    """Pairwise P/R/F1 from a contingency table rather than from pairs.

    Enumerating pairs is quadratic and n is in the thousands; the
    cross-tabulation is exact and linear. TP counts pairs inside one
    (system, gold) cell; the two margins give FP and FN.
    """
    n = len(system)
    cell, sys_size, gold_size = Counter(zip(system, gold)), Counter(system), Counter(gold)
    tp = sum(_choose2(c) for c in cell.values())
    fp = sum(_choose2(c) for c in sys_size.values()) - tp
    fn = sum(_choose2(c) for c in gold_size.values()) - tp
    tn = _choose2(n) - tp - fp - fn
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (2 * precision * recall / (precision + recall)
          if precision and recall else 0.0)
    return {"mentions": n, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": precision, "recall": recall, "f1": f1}


def bcubed(system: list, gold: list) -> dict:
    """B-cubed P/R/F1, averaged per mention so big clusters cannot dominate."""
    n = len(system)
    if not n:
        return {"mentions": 0, "precision": None, "recall": None, "f1": 0.0}
    cell, sys_size, gold_size = Counter(zip(system, gold)), Counter(system), Counter(gold)
    p = sum(cell[(s, g)] / sys_size[s] for s, g in zip(system, gold)) / n
    r = sum(cell[(s, g)] / gold_size[g] for s, g in zip(system, gold)) / n
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return {"mentions": n, "precision": p, "recall": r, "f1": f1}


# --------------------------------------------------------------------------
# one scoring pass over a label set
# --------------------------------------------------------------------------

def evaluate(mentions: list[dict], gold: dict, entity_person: dict,
             entity_by_id: dict, by_oid: dict, examples: int) -> dict:
    scored = [m for m in mentions if m["id"] in gold]
    resolved = [m for m in scored
                if m.get("status") == "resolved" and m.get("entity")]

    metrics = {
        "with_abstention_as_singletons": {
            "pairwise": pairwise([cluster_id(m) for m in scored],
                                 [gold[m["id"]] for m in scored]),
            "bcubed": bcubed([cluster_id(m) for m in scored],
                             [gold[m["id"]] for m in scored]),
        },
        "resolved_only": {
            "pairwise": pairwise([cluster_id(m) for m in resolved],
                                 [gold[m["id"]] for m in resolved]),
            "bcubed": bcubed([cluster_id(m) for m in resolved],
                             [gold[m["id"]] for m in resolved]),
        },
        "scored_mentions": len(scored),
        "scored_mentions_resolved": len(resolved),
        "scored_mentions_abstained": len(scored) - len(resolved),
    }

    # ---- false merges: one entity, several employees ------------------------
    per_entity: dict[int, list[dict]] = defaultdict(list)
    for mention in resolved:
        per_entity[mention["entity"]].append(mention)
    false_merges = []
    for eid, group in per_entity.items():
        labels = {gold[m["id"]] for m in group}
        if len(labels) < 2:
            continue
        entity = entity_by_id.get(eid, {})
        detail = [{
            "person": by_oid[oid]["name"],
            "title": by_oid[oid].get("title", ""),
            "mentions": sum(1 for m in group if gold[m["id"]] == oid),
            "surfaces": sorted({m["surface"] for m in group
                                if gold[m["id"]] == oid})[:4],
        } for oid in sorted(labels)]
        false_merges.append({
            "entity_id": eid, "entity_key": entity.get("key"),
            "entity_name": entity.get("name"),
            "people_fused": len(labels), "mentions_affected": len(group),
            "detail": sorted(detail, key=lambda d: -d["mentions"]),
        })
    false_merges.sort(key=lambda d: (-d["people_fused"], -d["mentions_affected"]))

    # ---- split identities: one employee, several entities -------------------
    per_person: dict[str, list[dict]] = defaultdict(list)
    for mention in resolved:
        per_person[gold[mention["id"]]].append(mention)
    splits = []
    for oid, group in per_person.items():
        eids = {m["entity"] for m in group}
        if len(eids) < 2:
            continue
        frags = [{"entity_key": entity_by_id.get(eid, {}).get("key"),
                  "entity_name": entity_by_id.get(eid, {}).get("name"),
                  "mentions": sum(1 for m in group if m["entity"] == eid)}
                 for eid in sorted(eids)]
        splits.append({"person": by_oid[oid]["name"], "email": oid,
                       "fragments": len(eids), "mentions": len(group),
                       "detail": sorted(frags, key=lambda d: -d["mentions"])})
    splits.sort(key=lambda d: (-d["fragments"], -d["mentions"]))

    # ---- precision by method ------------------------------------------------
    # A resolution is *decidable* when the entity it chose is itself matched to
    # an employee: then "same employee or not" is a fact. When the chosen entity
    # matches no employee the outcome is genuinely unknown — it may be an
    # unlinked fragment of the right person, or a real outsider — and is
    # reported apart rather than scored either way.
    counts: dict[str, Counter] = defaultdict(Counter)
    errors: dict[str, list[dict]] = defaultdict(list)
    for mention in resolved:
        method = mention.get("method") or "unknown"
        chosen = entity_person.get(mention["entity"])
        counts[method]["scored_mentions"] += 1
        if chosen is None:
            counts[method]["indeterminate"] += 1
        elif chosen == gold[mention["id"]]:
            counts[method]["correct"] += 1
        else:
            counts[method]["wrong"] += 1
            errors[method].append({
                "surface": mention["surface"],
                "chose": by_oid[chosen]["name"],
                "gold": by_oid[gold[mention["id"]]]["name"],
                "reason": (mention.get("reason") or "")[:120],
            })
    by_method = {}
    for method, c in sorted(counts.items()):
        decidable = c["correct"] + c["wrong"]
        by_method[method] = {
            "scored_mentions": c["scored_mentions"], "decidable": decidable,
            "correct": c["correct"], "wrong": c["wrong"],
            "indeterminate": c["indeterminate"],
            "precision": (c["correct"] / decidable) if decidable else None,
            "examples_wrong": errors[method][:examples],
        }

    return {"metrics": metrics,
            "false_merges": {"count": len(false_merges),
                             "mentions_affected": sum(f["mentions_affected"]
                                                      for f in false_merges),
                             "detail": false_merges},
            "split_identities": {"count": len(splits),
                                 "people_with_scored_mentions": len(per_person),
                                 "detail": splits},
            "precision_by_method": by_method}


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Score entity resolution against "
                                             "the identity oracle.")
    ap.add_argument("--run", default=None, help="run_id (default: newest)")
    ap.add_argument("--examples", type=int, default=6,
                    help="concrete examples to keep per finding")
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
    dropped_keys = [k for k, v in claims.items() if len(v) > 1]

    # How many employees' names contain a given token. This is what makes
    # "refusing was right" checkable.
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
            entity_multi.append({"entity_key": entity["key"],
                                 "entity_name": entity["name"],
                                 "people": sorted(by_oid[h]["name"] for h in hits)})

    # ---- mention -> employee ------------------------------------------------
    strict: dict[int, str] = {}
    lenient: dict[int, str] = {}
    sources = Counter()
    excluded = Counter()
    namesakes: list[dict] = []

    for mention in mentions:
        surface = mention.get("surface") or ""
        address = observed_email(mention)
        by_name, _ = match_person(name_keys(surface), index)

        if address is not None:
            keys = {("email", address)}
            internal = internal_domain(domain_of(address))
            if internal:
                keys |= name_keys(local_part(address))
            by_email, _ = match_person(keys, index)
            if by_email:
                if by_name and by_name != by_email:
                    excluded["labellers_disagreed"] += 1
                    continue
                strict[mention["id"]] = lenient[mention["id"]] = by_email
                sources["email_anchor"] += 1
                continue
            if not internal:
                # The document gives this person an address at another company.
                # `Priya Shah <priya.shah@mediscale.com>` is not the Redwood
                # employee of that name, and scoring her as one would commit the
                # exact false merge this evaluation exists to detect. Excluded
                # in both label sets, in either direction.
                if by_name:
                    namesakes.append({"surface": surface, "address": address,
                                      "would_have_been": by_oid[by_name]["name"]})
                    excluded["external_namesake"] += 1
                else:
                    excluded["address_outside_directory"] += 1
                continue
            # Internal address the directory does not list by address —
            # `grace@redwood.com` has a one-token local part. The full-name
            # surface on the same mention is then the identifying evidence.
            if by_name:
                strict[mention["id"]] = lenient[mention["id"]] = by_name
                sources["name_anchor_internal_address"] += 1
                continue
            excluded["internal_address_unmatched"] += 1
            continue

        if by_name:
            strict[mention["id"]] = lenient[mention["id"]] = by_name
            sources["name_anchor_no_address"] += 1
            continue

        # Lenient only: a one-token surface exactly one employee answers to.
        toks = tokens_of(surface)
        if len(toks) == 1:
            owners = token_owners.get(next(iter(toks)), set())
            if len(owners) == 1:
                lenient[mention["id"]] = next(iter(owners))
                excluded["lenient_only_single_token"] += 1
                continue
            excluded["surface_ambiguous_or_absent"] += 1
            continue
        excluded["surface_names_nobody_in_directory"] += 1

    strict_eval = evaluate(mentions, strict, entity_person, entity_by_id,
                           by_oid, args.examples)
    lenient_eval = evaluate(mentions, lenient, entity_person, entity_by_id,
                            by_oid, args.examples)

    # ---- entity-level split, independent of any mention label ---------------
    per_person_entities: dict[str, list[str]] = defaultdict(list)
    for eid, oid in entity_person.items():
        per_person_entities[oid].append(entity_by_id[eid]["key"])
    entity_level_splits = {by_oid[o]["name"]: sorted(k)
                           for o, k in per_person_entities.items() if len(k) > 1}

    # ---- gold-free false-merge sweep over *every* entity --------------------
    # The oracle sees 8% of the entity population, so a merge among the other
    # 92% would be invisible to every number above. This check needs no oracle:
    # if one entity's addresses carry two names that are neither equal nor one
    # a subset of the other, it has fused two people. It covers all 1,251
    # identities and is the only precision-flavoured statement here that is not
    # confined to the directory.
    disagreeing = []
    for entity in entities:
        sets = {t for t in (tokens_of(local_part(a)) for a in entity_emails(entity))
                if len(t) >= 2}
        if len(sets) < 2:
            continue
        smallest = min(sets, key=len)
        if not all(smallest <= t or t <= smallest for t in sets):
            disagreeing.append({"entity_key": entity["key"],
                                "names": sorted(sorted(t) for t in sets)})

    # ---- abstention ---------------------------------------------------------
    abstained = [m for m in mentions if m.get("status") == "unresolved"
                 and (m.get("candidates") or 0) > 1]
    buckets = Counter()
    bucket_examples: dict[str, list[str]] = defaultdict(list)
    for mention in abstained:
        toks = tokens_of(mention.get("surface") or "")
        owners: set[str] = set()
        if toks and all(t in token_owners for t in toks):
            owners = set.intersection(*(token_owners[t] for t in toks))
        if len(owners) > 1:
            bucket = "ambiguous_in_directory"
        elif len(owners) == 1:
            bucket = "unique_employee_existed"
        else:
            bucket = "no_employee_matches_surface"
        buckets[bucket] += 1
        if len(bucket_examples[bucket]) < args.examples:
            bucket_examples[bucket].append(mention.get("surface") or "")
    defensible = buckets["ambiguous_in_directory"] + buckets["no_employee_matches_surface"]
    abstention = {
        "unresolved_with_multiple_candidates": len(abstained),
        "buckets": dict(buckets),
        "examples": dict(bucket_examples),
        "defensible_rate": defensible / len(abstained) if abstained else None,
        "forced_by_directory_ambiguity_rate":
            buckets["ambiguous_in_directory"] / len(abstained) if abstained else None,
    }

    # ---- graph-evidence diagnostics, gold-free -----------------------------
    # The oracle cannot adjudicate this tier (see caveats), so two checks that
    # need no ground truth at all: how often it commits to somebody outside the
    # directory, and whether it contradicts itself inside one document, where
    # one handle can only mean one person.
    tier = [m for m in mentions if m.get("method") == "graph_evidence"
            and m.get("entity")]
    per_doc_surface: dict[tuple, set] = defaultdict(set)
    for mention in tier:
        per_doc_surface[(mention["dsid"], mention["normalised"])].add(mention["entity"])
    inconsistent = {k: v for k, v in per_doc_surface.items() if len(v) > 1}
    graph_tier = {
        "decisions": len(tier),
        "one_token_surfaces": sum(1 for m in tier
                                  if len(tokens_of(m["surface"] or "")) == 1),
        "landed_on_a_directory_employee": sum(1 for m in tier
                                              if m["entity"] in entity_person),
        "landed_outside_the_directory": sum(1 for m in tier
                                            if m["entity"] not in entity_person),
        "same_handle_same_document_conflicts": len(inconsistent),
        "conflicting_handles": sorted({k[1] for k in inconsistent})[:args.examples],
    }

    # ---- coverage -----------------------------------------------------------
    bots = sum(1 for m in mentions
               if (m.get("reason") or "").startswith("automation, not a person"))
    coverage = {
        "oracle_people": len(people),
        "graph_entities": len(entities),
        "graph_mentions": len(mentions),
        "entities_matched_to_an_employee": len(entity_person),
        "entities_matching_two_or_more_employees": len(entity_multi),
        "entities_out_of_scope": len(entities) - len(entity_person) - len(entity_multi),
        "mentions_with_strict_gold": len(strict),
        "mentions_with_strict_gold_pct": 100.0 * len(strict) / len(mentions) if mentions else 0.0,
        "mentions_with_lenient_gold": len(lenient),
        "mentions_with_lenient_gold_pct": 100.0 * len(lenient) / len(mentions) if mentions else 0.0,
        "mentions_excluded": len(mentions) - len(strict),
        "exclusion_reasons": dict(excluded),
        "gold_label_sources": dict(sources),
        "employees_seen_via_entities": len(set(entity_person.values())),
        "employees_seen_via_strict_mentions": len(set(strict.values())),
        "employees_seen_via_lenient_mentions": len(set(lenient.values())),
        "bot_mentions_the_system_already_skips": bots,
        "directory_keys_dropped_as_ambiguous":
            [[k[0], sorted(k[1]) if isinstance(k[1], frozenset) else k[1]]
             for k in dropped_keys],
        "external_namesake_examples": namesakes[:args.examples],
    }

    caveats = [
        "Strict precision is close to structurally guaranteed, not earned. "
        f"{sources.get('email_anchor', 0)} of {len(strict)} strict labels come "
        "from the same address the tier-1 rule keyed on, and the resolver only "
        "merges identities that share a full name — while every full name in "
        "the directory is unique. A false merge of two employees therefore "
        "cannot appear in this label set. Read strict recall, not strict "
        "precision.",
        "No abstained mention carries a strict label, so strict recall measures "
        "fragmentation only, never the cost of refusing. Every unresolved "
        "surface with two or more name tokens is a bot or a handle "
        "(`infra-bot`, `alice-tilman`), not a directory name. The lenient run "
        "is where abstention enters the recall figure.",
        "The graph-evidence tier is effectively unscorable against this oracle: "
        f"{graph_tier['one_token_surfaces']} of its {graph_tier['decisions']} "
        "decisions are on one-token surfaces, for which a directory of "
        "employees supplies no referent, and "
        f"{graph_tier['landed_outside_the_directory']} land on identities the "
        "directory does not contain. Its lenient precision rests on a small "
        "decidable sample; treat the sample size as the headline.",
        "Lenient precision is near-structural too. A lenient label is a "
        "function of the surface token, and the resolver's candidate set is a "
        "function of the same token, so two mentions can only land in one "
        "entity with different labels if their surfaces differ — which is "
        "exactly the case the label rule declines to cover. Zero false "
        "positives here is weak evidence, not strong evidence.",
        "The oracle covers only Redwood Inference employees. Customers, "
        "vendors and partners in this corpus are out of scope by construction, "
        "so a mention resolved to an outsider is never counted right or wrong. "
        f"It sees {len(entity_person)} of {len(entities)} entities; the "
        "gold-free sweep below is the only check that reaches the rest.",
    ]

    result = {
        "run_id": run_id,
        "oracle": str(ORACLE_PATH.relative_to(REPO)),
        "matching_rule": {
            "entity_to_employee":
                "An Entity matches an employee when their identity keys "
                "intersect on exactly one employee. Keys are (a) an exact email "
                "address, (b) the token set of a name or email local part of two "
                "or more tokens, accents and honorifics folded, and (c) the "
                "letters-only squash of the same, which settles "
                "O'Connor/oconnor and El-Sayed/elsayed. Name-shaped keys are "
                "taken from an Entity's addresses only when the address sits on "
                "a domain whose root starts with 'redwood', so "
                "priya.sharma@mediloop.com cannot be read as the Redwood "
                "employee of that name. A key claimed by two employees is "
                "dropped from the index; an Entity hitting two employees is "
                "recorded as a false merge and mapped to neither.",
            "mention_to_employee_strict":
                "In order: (1) the address the document attached to the mention, "
                "when it matches an employee; (2) an internal address plus a "
                "full-name surface, which covers grace@redwood.com; (3) a "
                "full-name surface on a mention carrying no address. A mention "
                "whose address is external is excluded outright even when its "
                "surface matches an employee's name — that is the namesake trap. "
                "Where both labellers speak and disagree the mention is dropped.",
            "mention_to_employee_lenient":
                "Strict, plus a one-token surface that exactly one employee "
                "answers to. Assumes the referent is an employee, which this "
                "corpus does not guarantee. Never used for a headline number.",
            "clusters":
                "System cluster = the Entity a Mention resolved to; an "
                "unresolved mention is a singleton, so abstention costs recall. "
                "Gold cluster = the employee.",
        },
        "coverage": coverage,
        "strict": strict_eval,
        "lenient": lenient_eval,
        "entities_matching_two_or_more_employees": entity_multi,
        "entity_level_splits": {
            "employees_with_more_than_one_entity": len(entity_level_splits),
            "entities_per_covered_employee":
                (len(entity_person) / len(set(entity_person.values()))
                 if entity_person else None),
            "detail": entity_level_splits,
        },
        "gold_free_false_merge_sweep": {
            "entities_checked": len(entities),
            "entities_whose_addresses_disagree_on_the_name": len(disagreeing),
            "detail": disagreeing[:args.examples],
        },
        "abstention": abstention,
        "graph_evidence_diagnostics": graph_tier,
        "caveats": caveats,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    report(result, args.examples)
    return 0


def _pct(x: float | None) -> str:
    return "  n/a" if x is None else f"{100 * x:5.1f}%"


def _metrics_block(title: str, block: dict) -> None:
    print(f"\n-- {title} --")
    for label, key in (("abstention counted (unresolved = singleton)",
                        "with_abstention_as_singletons"),
                       ("resolved mentions only", "resolved_only")):
        pw, b3 = block[key]["pairwise"], block[key]["bcubed"]
        print(f"  {label}")
        print(f"    pairwise  P {_pct(pw['precision'])}  R {_pct(pw['recall'])}"
              f"  F1 {_pct(pw['f1'])}   TP {pw['tp']} FP {pw['fp']} "
              f"FN {pw['fn']} TN {pw['tn']}")
        print(f"    B-cubed   P {_pct(b3['precision'])}  R {_pct(b3['recall'])}"
              f"  F1 {_pct(b3['f1'])}   over {b3['mentions']} mentions")


def report(r: dict, examples: int) -> None:
    c = r["coverage"]
    print(f"\n=== identity evaluation — run {r['run_id']} ===")
    print(f"oracle: {r['oracle']} (read here and nowhere else)")

    print("\n-- coverage ------------------------------------------------------")
    print(f"  mentions in graph              {c['graph_mentions']:>6}")
    print(f"  ... strict ground truth        {c['mentions_with_strict_gold']:>6}"
          f"  ({c['mentions_with_strict_gold_pct']:.1f}%)   {c['gold_label_sources']}")
    print(f"  ... lenient ground truth       {c['mentions_with_lenient_gold']:>6}"
          f"  ({c['mentions_with_lenient_gold_pct']:.1f}%)")
    print(f"  ... excluded from strict       {c['mentions_excluded']:>6}")
    for reason, n in sorted(c["exclusion_reasons"].items(), key=lambda kv: -kv[1]):
        print(f"        {reason:<34}{n:>6}")
    print(f"        (of the excluded, {c['bot_mentions_the_system_already_skips']}"
          " are bots the system already refuses)")
    print(f"  entities in graph              {c['graph_entities']:>6}"
          f"   matched to an employee {c['entities_matched_to_an_employee']}"
          f"   out of scope {c['entities_out_of_scope']}")
    print(f"  employees in directory         {c['oracle_people']:>6}"
          f"   seen via entities {c['employees_seen_via_entities']}"
          f"   via strict mentions {c['employees_seen_via_strict_mentions']}"
          f"   via lenient {c['employees_seen_via_lenient_mentions']}")
    if c["external_namesake_examples"]:
        print("  namesakes excluded (directory name, another company's address):")
        for e in c["external_namesake_examples"][:4]:
            print(f"        {e['surface']!r} <{e['address']}> "
                  f"— not {e['would_have_been']}")

    _metrics_block("STRICT gold — the headline", r["strict"]["metrics"])
    _metrics_block("LENIENT gold — indicative only, assumes employee referents",
                   r["lenient"]["metrics"])

    for name in ("strict", "lenient"):
        ev = r[name]
        fm, sp = ev["false_merges"], ev["split_identities"]
        print(f"\n-- {name}: false merges — {fm['count']} entities fuse two or "
              f"more employees ({fm['mentions_affected']} mentions) --")
        for item in fm["detail"][:examples]:
            print(f"  {item['entity_key']}  [{item['entity_name']}]")
            for d in item["detail"]:
                print(f"        {d['person']:<24} x{d['mentions']:<4} "
                      f"surfaces {d['surfaces']}")
        print(f"-- {name}: split identities — {sp['count']} of "
              f"{sp['people_with_scored_mentions']} employees spread across "
              "several entities --")
        for item in sp["detail"][:examples]:
            print(f"  {item['person']:<22} {item['fragments']} entities, "
                  f"{item['mentions']} mentions")
            for d in item["detail"][:4]:
                print(f"        {str(d['entity_key']):<46} x{d['mentions']}")

    els = r["entity_level_splits"]
    print("\n-- fragmentation, counted on entities rather than mentions --")
    print(f"  {c['entities_matched_to_an_employee']} entities cover "
          f"{c['employees_seen_via_entities']} employees "
          f"({els['entities_per_covered_employee']:.2f} entities each); "
          f"{els['employees_with_more_than_one_entity']} employees are split")

    if r["entities_matching_two_or_more_employees"]:
        print("\n-- entities whose own identity keys hit two employees --")
        for item in r["entities_matching_two_or_more_employees"][:examples]:
            print(f"  {item['entity_key']} -> {item['people']}")

    sweep = r["gold_free_false_merge_sweep"]
    print("\n-- gold-free false-merge sweep over every entity, in or out of "
          "the directory --")
    print(f"  {sweep['entities_checked']} entities checked; "
          f"{sweep['entities_whose_addresses_disagree_on_the_name']} carry "
          "addresses that disagree about the person's name")
    for item in sweep["detail"]:
        print(f"        {item['entity_key']} -> {item['names']}")

    for name in ("strict", "lenient"):
        print(f"\n-- precision by resolution method ({name} gold) --")
        print(f"  {'method':<22}{'scored':>8}{'decidable':>11}{'correct':>9}"
              f"{'wrong':>7}{'indet':>7}{'precision':>11}")
        for method, s in r[name]["precision_by_method"].items():
            print(f"  {method:<22}{s['scored_mentions']:>8}{s['decidable']:>11}"
                  f"{s['correct']:>9}{s['wrong']:>7}{s['indeterminate']:>7}"
                  f"{_pct(s['precision']):>11}")
        for method, s in r[name]["precision_by_method"].items():
            for e in s["examples_wrong"][:2]:
                print(f"      wrong [{method}] {e['surface']!r} -> {e['chose']}"
                      f"  (gold {e['gold']})")

    g = r["graph_evidence_diagnostics"]
    print("\n-- graph-evidence tier, gold-free diagnostics --")
    print(f"  decisions {g['decisions']}, of which {g['one_token_surfaces']} on "
          "one-token surfaces")
    print(f"  landed on a directory employee   {g['landed_on_a_directory_employee']}")
    print(f"  landed outside the directory     {g['landed_outside_the_directory']}")
    print(f"  same handle, same document, two different entities: "
          f"{g['same_handle_same_document_conflicts']}   "
          f"{g['conflicting_handles']}")

    a = r["abstention"]
    print(f"\n-- abstention — {a['unresolved_with_multiple_candidates']} mentions "
          "refused while holding several candidates --")
    for bucket, n in sorted(a["buckets"].items(), key=lambda kv: -kv[1]):
        print(f"  {bucket:<32}{n:>6}   e.g. {a['examples'].get(bucket, [])[:4]}")
    print(f"  refusal defensible (directory ambiguous, or names no employee) "
          f"{_pct(a['defensible_rate'])}")
    print(f"  refusal forced by directory ambiguity alone                   "
          f"{_pct(a['forced_by_directory_ambiguity_rate'])}")

    print("\n-- caveats -------------------------------------------------------")
    for i, caveat in enumerate(r["caveats"], 1):
        print(f"  {i}. {caveat}")

    print(f"\nwrote {OUT_PATH.relative_to(REPO)}")


if __name__ == "__main__":
    raise SystemExit(main())
