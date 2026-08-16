"""Contract tests for deterministic ids and the collision registry.

No live HydraDB is needed: everything here is pure hashing plus a temporary
SQLite file.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tracegraph.ids import (
    ID_MASK,
    KIND_EDGE,
    KIND_NODE,
    IdCollisionError,
    IdRegistry,
    IdRow,
    KeyCase,
    canonical_key,
    edge_full_hash,
    edge_id,
    edge_identity,
    key_case,
    node_full_hash,
    node_id,
    node_identity,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# --- Determinism ------------------------------------------------------------


def test_ids_are_stable_within_a_process() -> None:
    assert node_id("person", "sam altman") == node_id("person", "sam altman")
    assert edge_id("MENTIONED_IN", 7, 9, "span:3") == edge_id(
        "MENTIONED_IN", 7, 9, "span:3"
    )


_SUBPROCESS_SNIPPET = (
    "from tracegraph.ids import edge_id, node_id;"
    "print(node_id('person', 'sam altman'));"
    "print(edge_id('MENTIONED_IN', 7, 9, 'span:3'))"
)


@pytest.mark.parametrize("hash_seed", ["0", "1", "982451653", "random"])
def test_ids_are_stable_across_processes(hash_seed: str) -> None:
    """A builtin hash() anywhere in the derivation would move under this."""
    env = dict(os.environ, PYTHONHASHSEED=hash_seed, PYTHONPATH=str(REPO_ROOT))
    result = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS_SNIPPET],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [
        str(node_id("person", "sam altman")),
        str(edge_id("MENTIONED_IN", 7, 9, "span:3")),
    ]


def test_full_hash_is_a_complete_sha256_digest() -> None:
    digest = node_full_hash("document", "dsid_00042")
    assert len(digest) == 64
    assert int(digest, 16) >= 0
    # The id is the first eight bytes of that same digest, masked.
    assert node_id("document", "dsid_00042") == int(digest[:16], 16) & ID_MASK
    assert len(edge_full_hash("ASSERTS", 1, 2)) == 64


# --- Id range ---------------------------------------------------------------


def test_ids_are_non_negative_and_fit_63_bits() -> None:
    for i in range(2000):
        assert 0 <= node_id("document", f"dsid_{i}") <= ID_MASK
        assert 0 <= edge_id("MENTIONED_IN", i, i + 1, str(i)) <= ID_MASK


def test_edge_id_range_check_rejects_out_of_range_endpoints() -> None:
    with pytest.raises(ValueError):
        edge_id("MENTIONED_IN", -1, 2)
    with pytest.raises(ValueError):
        edge_id("MENTIONED_IN", 1, ID_MASK + 1)


# --- Separator --------------------------------------------------------------


def test_unit_separator_keeps_type_and_key_apart() -> None:
    assert node_id("a", "bc") != node_id("ab", "c")


def test_keys_carrying_the_separator_are_rejected() -> None:
    with pytest.raises(ValueError):
        node_id("person", "a\x1fb")
    with pytest.raises(ValueError):
        canonical_key("person", "a\x1fb")
    with pytest.raises(ValueError):
        edge_id("MENTIONED_IN", 1, 2, "scope\x1fmore")


def test_edge_components_are_positional() -> None:
    assert edge_id("MENTIONED_IN", 1, 2) != edge_id("MENTIONED_IN", 2, 1)
    assert edge_id("MENTIONED_IN", 1, 2) != edge_id("MENTIONED_IN", 1, 2, "scope")
    assert edge_id("MENTIONED_IN", 1, 2) != edge_id("RESOLVES_TO", 1, 2)


# --- Canonicalisation -------------------------------------------------------


def test_case_insensitive_types_collapse() -> None:
    assert key_case("person") is KeyCase.FOLD
    for node_type in ("person", "alias", "email", "handle"):
        assert canonical_key(node_type, "Sam Altman") == canonical_key(
            node_type, "sam altman"
        )
        assert node_identity(node_type, "SAM ALTMAN").id == node_identity(
            node_type, "sam altman"
        ).id


def test_case_sensitive_types_do_not_collapse() -> None:
    assert key_case("document") is KeyCase.PRESERVE
    for node_type in ("document", "dsid", "ticket"):
        assert canonical_key(node_type, "ENG-1234") != canonical_key(
            node_type, "eng-1234"
        )
        assert node_identity(node_type, "ENG-1234").id != node_identity(
            node_type, "eng-1234"
        ).id


def test_unknown_types_preserve_case() -> None:
    """The safe default: a split identity is repairable, a merged one is not."""
    assert key_case("some_type_nobody_registered") is KeyCase.PRESERVE


def test_policy_lookup_tolerates_type_spelling() -> None:
    assert key_case("ClaimGroup") is key_case("claim_group") is KeyCase.PRESERVE
    assert key_case("Person") is key_case("PERSON") is KeyCase.FOLD


def test_whitespace_is_stripped_and_collapsed_for_every_type() -> None:
    assert canonical_key("document", "  dsid \t 42\n") == "dsid 42"
    assert canonical_key("person", " Sam   Altman ") == "sam altman"


def test_canonical_key_is_idempotent() -> None:
    raw = "  Sam   ALTMAN "
    once = canonical_key("person", raw)
    assert canonical_key("person", once) == once


def test_empty_keys_are_rejected() -> None:
    with pytest.raises(ValueError):
        canonical_key("person", "   ")
    with pytest.raises(ValueError):
        node_id("person", "")


def test_node_identity_canonicalises_before_hashing() -> None:
    identity = node_identity("person", "  Sam   Altman ")
    assert identity.natural_key == "sam altman"
    assert identity.kind == KIND_NODE
    assert identity.id == node_id("person", "sam altman")
    assert identity.full_hash == node_full_hash("person", "sam altman")


def test_edge_identity_round_trips_its_payload() -> None:
    identity = edge_identity("MENTIONED_IN", 7, 9, "span:3")
    assert identity.kind == KIND_EDGE
    assert identity.node_type == "MENTIONED_IN"
    assert identity.natural_key == "7\x1f9\x1fspan:3"
    assert identity.id == edge_id("MENTIONED_IN", 7, 9, "span:3")


# --- Registry ---------------------------------------------------------------


@pytest.fixture()
def registry(tmp_path: Path):
    with IdRegistry(tmp_path / "registry.sqlite3") as reg:
        yield reg


def test_registry_stores_and_looks_up(registry: IdRegistry) -> None:
    identity = node_identity("document", "dsid_00042")
    assert registry.register(*identity) is True
    assert registry.lookup(identity.id) == identity
    assert registry.lookup(identity.id + 1) is None
    assert registry.count() == 1
    assert registry.count(KIND_NODE) == 1
    assert registry.count(KIND_EDGE) == 0


def test_reregistering_the_same_identity_is_a_no_op(registry: IdRegistry) -> None:
    identity = node_identity("document", "dsid_00042")
    assert registry.register(*identity) is True
    assert registry.register(*identity) is False
    assert registry.register_many([identity, identity]) == 0
    assert registry.count() == 1


def test_register_many_counts_only_new_rows(registry: IdRegistry) -> None:
    first = [node_identity("document", f"dsid_{i}") for i in range(1000)]
    assert registry.register_many(first) == 1000
    second = first + [node_identity("document", "dsid_new")]
    assert registry.register_many(second) == 1
    assert registry.count() == 1001


def test_registry_survives_a_reopen(tmp_path: Path) -> None:
    path = tmp_path / "registry.sqlite3"
    identity = node_identity("document", "dsid_00042")
    with IdRegistry(path) as reg:
        assert reg.register(*identity) is True
    with IdRegistry(path) as reg:
        assert reg.lookup(identity.id) == identity
        assert reg.register(*identity) is False


def test_forced_collision_raises(registry: IdRegistry) -> None:
    """Two different natural keys of the same type at one id."""
    stored = node_identity("person", "sam altman")
    other = node_identity("person", "soham parekh")
    assert stored.id != other.id
    registry.register(*stored)

    forged = other._replace(id=stored.id)
    with pytest.raises(IdCollisionError) as excinfo:
        registry.register(*forged)

    error = excinfo.value
    assert error.stored == stored
    assert error.offered == forged
    assert "sam altman" in str(error)
    assert "soham parekh" in str(error)
    assert stored.full_hash in str(error)
    # The loser of the collision must not have overwritten the winner.
    assert registry.lookup(stored.id) == stored
    assert registry.count() == 1


def test_cross_type_collision_raises(registry: IdRegistry) -> None:
    """HydraDB resolves a node by id alone, so the label is no defence."""
    stored = node_identity("document", "dsid_00042")
    registry.register(*stored)
    forged = node_identity("person", "sam altman")._replace(id=stored.id)
    with pytest.raises(IdCollisionError):
        registry.register(*forged)
    assert registry.lookup(stored.id) == stored


def test_cross_kind_collision_raises(registry: IdRegistry) -> None:
    """A node id and an edge id that fold together is still one address."""
    stored = node_identity("document", "dsid_00042")
    registry.register(*stored)
    forged = edge_identity("MENTIONED_IN", 7, 9)._replace(id=stored.id)
    with pytest.raises(IdCollisionError):
        registry.register(*forged)
    assert registry.lookup(stored.id) == stored
    assert registry.count(KIND_EDGE) == 0


def test_a_colliding_batch_commits_nothing(registry: IdRegistry) -> None:
    stored = node_identity("document", "dsid_00042")
    registry.register(*stored)
    batch = [
        node_identity("document", "dsid_00043"),
        node_identity("person", "sam altman")._replace(id=stored.id),
        node_identity("document", "dsid_00044"),
    ]
    with pytest.raises(IdCollisionError):
        registry.register_many(batch)
    assert registry.count() == 1


def test_collision_inside_one_batch_raises(registry: IdRegistry) -> None:
    stored = node_identity("document", "dsid_00042")
    batch = [stored, node_identity("person", "sam altman")._replace(id=stored.id)]
    with pytest.raises(IdCollisionError):
        registry.register_many(batch)
    assert registry.count() == 0


def test_registry_rejects_malformed_rows(registry: IdRegistry) -> None:
    identity = node_identity("document", "dsid_00042")
    with pytest.raises(ValueError):
        registry.register(*identity._replace(kind="vertex"))
    with pytest.raises(ValueError):
        registry.register(*identity._replace(id=-1))
    with pytest.raises(ValueError):
        registry.register(*identity._replace(id=ID_MASK + 1))
    assert registry.count() == 0


def test_register_many_accepts_plain_tuples(registry: IdRegistry) -> None:
    identity = node_identity("document", "dsid_00042")
    assert registry.register_many([tuple(identity)]) == 1
    assert registry.lookup(identity.id) == IdRow(*identity)


def test_registering_nothing_is_allowed(registry: IdRegistry) -> None:
    assert registry.register_many([]) == 0
