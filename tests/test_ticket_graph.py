"""Ticket keys as cross-document structure.

Every parser has always extracted ticket keys into `ParsedDoc.references`, and
until now nothing read them. They are the one exact, inference-free link between
documents this corpus offers — a Slack thread and a Jira export that both name
`PROJ-412` are discussing the same work, and no amount of shared vocabulary
establishes that, because they share almost none.

The filter is the whole risk. `TICKET_KEY_RE` is `[A-Z]{2,10}-\\d{1,6}`, which
is the correct shape for a ticket and also the correct shape for `AES-256`. A
false ticket node joins two documents that have nothing to do with each other,
and the traversal that reads it would then present that as a connection — so
these tests are mostly about what must *not* become a ticket.
"""

from __future__ import annotations

import pytest

from tracegraph.ingest import _ticket_keys
from tracegraph.parsers.base import Reference, extract_references


def ref(target: str) -> Reference:
    return Reference(target=target, kind="ticket", start=0, end=len(target))


class TestRealTicketsSurvive:
    @pytest.mark.parametrize("key", [
        "PROJ-412", "ENG-1", "INC-9821", "AB-123456", "PLATFORM-77",
    ])
    def test_a_plausible_key_is_kept(self, key):
        assert _ticket_keys([ref(key)]) == [key]

    def test_keys_are_upper_cased(self):
        """`proj-412` and `PROJ-412` are one ticket, so they must be one node."""
        assert _ticket_keys([ref("proj-412")]) == ["PROJ-412"]

    def test_repeats_collapse_to_one(self):
        """The edge means "refers to"; saying it five times does not make it truer."""
        assert _ticket_keys([ref("PROJ-412")] * 5) == ["PROJ-412"]

    def test_order_is_preserved(self):
        keys = _ticket_keys([ref("ENG-2"), ref("ENG-1"), ref("ENG-2")])
        assert keys == ["ENG-2", "ENG-1"]

    def test_urls_are_not_tickets(self):
        url = Reference(target="https://example.com/x", kind="url", start=0, end=1)
        assert _ticket_keys([url]) == []


class TestFalsePositivesAreRefused:
    @pytest.mark.parametrize("key", [
        "AES-256", "SHA-256", "SHA-1", "RSA-2048", "MD-5", "TLS-13",
        "SOC-2", "PCI-3", "ISO-27001", "RFC-2119", "CVE-2026", "SEV-1",
        "UTF-8", "HTTP-2", "USD-100",
    ])
    def test_a_standard_or_cipher_is_not_a_ticket(self, key):
        """These have a ticket's exact shape and none of its meaning."""
        assert _ticket_keys([ref(key)]) == []

    @pytest.mark.parametrize("key", ["INT-4501", "SLA-99", "FP-16", "GPU-4"])
    def test_an_ambiguous_prefix_is_now_kept(self, key):
        """Deliberately reversed after review: every one of these is a plausible
        real project key — `INT` for Integrations is common — and excluding them
        lost real tickets to avoid hypothetical noise. The asymmetry runs the
        other way here: a missed ticket silently removes a cross-document link,
        while a spurious one adds an edge the >=2-document rule then ignores."""
        assert _ticket_keys([ref(key)]) == [key]

    @pytest.mark.parametrize("key", ["INC-2026", "REL-1999", "FY-2025"])
    def test_a_year_shaped_number_is_refused_for_date_prefixes(self, key):
        """`INC-2026` is a date, not the two-thousand-and-twenty-sixth incident."""
        assert _ticket_keys([ref(key)]) == []

    @pytest.mark.parametrize("key", ["ENG-1999", "PROJ-2024", "ENG-2100"])
    def test_a_year_shaped_number_survives_a_normal_prefix(self, key):
        """The year rule used to apply to every prefix, which silently discarded
        `ENG-1999` — and `ENG-` is the most common prefix in this corpus, so the
        blind spot was live rather than theoretical."""
        assert _ticket_keys([ref(key)]) == [key]

    def test_a_four_digit_counter_outside_year_range_is_kept(self):
        """Real counters do reach four digits; only year-like ones are dropped."""
        assert _ticket_keys([ref("ENG-1234")]) == ["ENG-1234"]
        assert _ticket_keys([ref("ENG-9821")]) == ["ENG-9821"]

    def test_a_mixed_document_keeps_only_the_real_ones(self):
        refs = [ref("AES-256"), ref("PROJ-412"), ref("SOC-2"), ref("ENG-77")]
        assert _ticket_keys(refs) == ["PROJ-412", "ENG-77"]


class TestAgainstRealText:
    def test_extraction_and_filtering_compose(self):
        """The two halves have to agree: what the regex finds, the filter judges."""
        content = (
            "Rolling back PROJ-412 after the AES-256 rekey broke SOC-2 evidence. "
            "See also ENG-1234 and the INC-2026 postmortem."
        )
        keys = _ticket_keys(extract_references(content))
        assert keys == ["PROJ-412", "ENG-1234"]

    def test_a_document_with_no_tickets_writes_no_edges(self):
        content = "The rollout was paused after P95 latency regressed to 1.8s."
        assert _ticket_keys(extract_references(content)) == []
