"""Parser tests, written against defects the corpus actually produced.

Each case here corresponds to something that was silently creating wrong
entities before it was found: escaped bodies, bot handles, offsets that did not
reproduce their surface, and a name capture that ran across a line break.
"""

from __future__ import annotations

import pytest

from tracegraph.parsers import parse_document
from tracegraph.parsers.base import (
    BOT,
    PERSON,
    looks_like_bot,
    name_tokens,
    normalise_content,
)


class TestNormaliseContent:
    def test_plain_text_is_unchanged(self):
        body = "From: A <a@b.com>\nTo: c@d.com\n\nhello"
        assert normalise_content(body) == body

    def test_json_encoded_body_is_decoded(self):
        raw = '["From: Grace <g@x.com>\\nTo: a@b.com"]'
        assert normalise_content(raw) == "From: Grace <g@x.com>\nTo: a@b.com"

    def test_escaped_newlines_without_json_are_unescaped(self):
        assert normalise_content("From: A <a@b.com>\\nTo: c@d.com") == (
            "From: A <a@b.com>\nTo: c@d.com"
        )

    def test_escaped_quotes_are_unescaped(self):
        assert normalise_content("O\\'Connor\\nnext") == "O'Connor\nnext"

    def test_is_idempotent(self):
        """Offsets index into this output, so applying it twice must not move them."""
        raw = "[\"From: Grace O\\'Connor <g@x.com>\\nSubject: hi\"]"
        once = normalise_content(raw)
        assert normalise_content(once) == once


class TestGmail:
    HEADERS = (
        "From: Alyssa Chen <alyssa.chen@cascadefg.com>\n"
        "To: Markus Klein <markus.klein@redwoodinference.com>\n"
        "Cc: Tom Becker <tom.becker@cascadefg.com>, Rachel Kim <rachel.kim@redwood.com>\n"
        "Date: Tue, Jun 3, 2025 at 9:12 AM\n"
        "Subject: Escalation\n\n"
        "Body text here.\n"
    )

    def test_extracts_name_and_address_pairs(self):
        doc = parse_document("d1", "gmail", "Escalation", self.HEADERS)
        by_surface = {m.surface: m for m in doc.mentions}
        assert "Alyssa Chen" in by_surface
        assert by_surface["Alyssa Chen"].attributes["email"] == "alyssa.chen@cascadefg.com"
        assert by_surface["Alyssa Chen"].attributes["domain"] == "cascadefg.com"

    def test_every_offset_reproduces_its_surface(self):
        doc = parse_document("d1", "gmail", "Escalation", self.HEADERS)
        assert doc.mentions
        assert len(doc.verified_mentions(self.HEADERS)) == len(doc.mentions)

    def test_second_recipient_has_no_leading_whitespace(self):
        """A comma-separated list must not leave ' Rachel Kim' as a name."""
        doc = parse_document("d1", "gmail", "Escalation", self.HEADERS)
        assert all(m.surface == m.surface.strip() for m in doc.mentions)

    def test_name_capture_does_not_cross_a_line_break(self):
        """`\\nTo: Sam Wilson` was a real entity this produced."""
        doc = parse_document("d1", "gmail", "x", self.HEADERS)
        assert all("\n" not in m.surface for m in doc.mentions)
        assert all(not m.surface.lower().startswith(("to:", "cc:", "from:"))
                   for m in doc.mentions)

    def test_quoted_reply_headers_are_not_attributed_to_this_document(self):
        body = self.HEADERS + "\n-----\nFrom: Older Sender <old@x.com>\n"
        doc = parse_document("d1", "gmail", "x", body)
        assert "Older Sender" not in {m.surface for m in doc.mentions}

    def test_json_encoded_document_parses(self):
        raw = "[\"From: Grace O\\'Connor <g@x.com>\\nSubject: hi\"]"
        doc = parse_document("d1", "gmail", "x", raw)
        body = normalise_content(raw)
        assert len(doc.verified_mentions(body)) == len(doc.mentions)
        assert any(m.surface == "Grace O'Connor" for m in doc.mentions)


class TestSlack:
    TRANSCRIPT = (
        "sasha: latency is up on prod\n"
        "deploy-bot: build 412 promoted\n"
        "kevin: any noisy neighbours?\n"
        "```\n"
        "note: this line is log output, not speech\n"
        "```\n"
        "maria: pushing a fix\n"
    )

    def test_speakers_become_person_mentions(self):
        doc = parse_document("d1", "slack", "eng-runtime", self.TRANSCRIPT)
        speakers = {m.surface for m in doc.mentions if m.role == "speaker"}
        assert {"sasha", "kevin", "maria"} <= speakers

    def test_bots_are_not_people(self):
        doc = parse_document("d1", "slack", "eng-runtime", self.TRANSCRIPT)
        bots = {m.surface for m in doc.mentions if m.kind == BOT}
        assert "deploy-bot" in bots
        assert all(m.kind == PERSON for m in doc.mentions if m.surface == "sasha")

    def test_code_fence_contents_are_not_speech(self):
        doc = parse_document("d1", "slack", "eng-runtime", self.TRANSCRIPT)
        assert "note" not in {m.surface for m in doc.mentions}

    def test_channel_is_an_attribute_not_a_mention(self):
        """The channel comes from the title, so it has no offsets in the body."""
        doc = parse_document("d1", "slack", "eng-runtime", self.TRANSCRIPT)
        assert doc.attributes["channel"] == "eng-runtime"
        assert len(doc.verified_mentions(self.TRANSCRIPT)) == len(doc.mentions)


class TestFireflies:
    TRANSCRIPT = (
        "summary:\nDiscovery call.\n\ntranscript:\nMeeting Header:\n"
        "Date: 2025-03-27\nDuration: 62 minutes\n"
        "Attendees: Maya Patel (Redwood AE); Jonas Reed (Redwood SE); "
        "Sofia Alvarez (Tethys CTO)\n"
    )

    def test_attendees_carry_organisation_and_role(self):
        doc = parse_document("d1", "fireflies", "Discovery", self.TRANSCRIPT)
        by_surface = {m.surface: m for m in doc.mentions}
        assert by_surface["Maya Patel"].attributes["organisation"] == "Redwood"
        assert by_surface["Maya Patel"].attributes["role"] == "AE"
        assert by_surface["Sofia Alvarez"].attributes["organisation"] == "Tethys"

    def test_offsets_verify(self):
        doc = parse_document("d1", "fireflies", "Discovery", self.TRANSCRIPT)
        assert len(doc.verified_mentions(self.TRANSCRIPT)) == len(doc.mentions)

    def test_truncated_attendee_is_dropped(self):
        body = self.TRANSCRIPT.replace("Sofia Alvarez (Tethys CTO)", "Sofia Alv...")
        doc = parse_document("d1", "fireflies", "Discovery", body)
        assert not any("Alv..." in m.surface for m in doc.mentions)


class TestReferences:
    def test_ticket_keys_are_extracted(self):
        body = "description:\nRelated work: Linear ENG-4129, ENG-4187."
        doc = parse_document("d1", "github", "PR", body)
        assert {r.target for r in doc.references} >= {"ENG-4129", "ENG-4187"}

    def test_ordinary_prose_is_not_a_ticket_key(self):
        doc = parse_document("d1", "github", "PR", "description:\nWe hit I-5 traffic.")
        assert not [r for r in doc.references if r.kind == "ticket"]


@pytest.mark.parametrize(
    "handle,expected",
    [("deploy-bot", True), ("ci-bot", True), ("infra-jenkins-bot", True),
     ("sam", False), ("maya", False), ("marta_kyc", False)],
)
def test_bot_detection(handle, expected):
    assert looks_like_bot(handle) is expected


def test_name_tokens_bridge_email_and_display_name():
    """The bridge entity resolution depends on."""
    assert name_tokens("alyssa.chen") == name_tokens("Alyssa Chen") == {"alyssa", "chen"}
    assert name_tokens("grace_oconnor") == {"grace", "oconnor"}
    # A single initial must not become a token, or every J matches every J.
    assert "j" not in name_tokens("J Smith")
