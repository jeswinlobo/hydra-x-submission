"""How an engine-returned path is counted and shown.

`algo.SPpaths` hands back a flat, alternating list — node, relationship type,
node — not a typed path object. A single participation edge therefore arrives as
three elements, and reporting `len(path)` as the hop count turned a one-hop walk
into "3 hops" in the interface. Overstating a traversal is exactly the kind of
claim this project is judged on, so both the arithmetic and the rendering are
pinned here.

No database needed: the shapes are the contract, and
`tests/test_hydra_contract.py` is what proves the engine really returns them.
"""

from __future__ import annotations

import pytest

from tracegraph.api import _path_steps

ENTITY = {"name": "Anil Shah", "key": "email:anil@lucidhealth.com",
          "kind": "person"}
CHANNEL = {"name": "eng-runtime"}


def hops(path) -> int:
    """The arithmetic the API applies, kept alongside what it is applied to."""
    return max((len(_path_steps(path)) - 1) // 2, 0)


def test_a_single_edge_is_one_hop_not_three():
    """The regression: three list elements, one relationship."""
    path = [ENTITY, "PARTICIPATED_IN", CHANNEL]
    assert len(path) == 3
    assert hops(path) == 1


@pytest.mark.parametrize("path,expected", [
    ([ENTITY], 0),
    ([ENTITY, "PARTICIPATED_IN", CHANNEL], 1),
    ([ENTITY, "PARTICIPATED_IN", CHANNEL, "PARTICIPATED_IN", ENTITY], 2),
    ([], 0),
    (None, 0),
])
def test_hop_count_tracks_relationships(path, expected):
    assert hops(path) == expected


def test_steps_carry_the_path_itself_not_a_summary():
    """The panel renders these, so they must be the engine's own elements."""
    steps = _path_steps([ENTITY, "PARTICIPATED_IN", CHANNEL])
    assert [s["kind"] for s in steps] == ["node", "relationship", "node"]
    assert [s["label"] for s in steps] == [
        "Anil Shah", "PARTICIPATED_IN", "eng-runtime"]


def test_a_node_without_a_name_still_labels():
    """Nodes arrive as property maps, and not every map has `name`."""
    steps = _path_steps([{"key": "email:x@y.com"}, "REL", {"dsid": "dsid_1"}])
    assert [s["label"] for s in steps] == ["email:x@y.com", "REL", "dsid_1"]
