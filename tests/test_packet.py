"""The canonical judgment packet (galley/packet.py): export template + the
model-free import, and its four hard refusals (anchoring, atomicity, channel,
intent zone)."""
from __future__ import annotations

import copy

import pytest

from docproof.flights import Cluster, Proposal
from galley.packet import (ACTIONS, PacketError, export_packet,
                           import_decisions)


def _cluster(para_text="It was very unique indeed.", start=7, end=18):
    # "very unique" occupies [7:18] in the default para_text.
    assert para_text[start:end] == "very unique"
    opt = Proposal(para_id="body-0001", start=start, end=end,
                   original="very unique", replacement="unique",
                   rationale="'unique' is absolute", model="gpt-5.6-luna",
                   lens="economy")
    return Cluster(para_id="body-0001", start=start, end=end,
                   original="very unique", sentence=para_text,
                   para_text=para_text, options=[opt])


def test_export_builds_a_template_with_stable_ids_and_options():
    pkt = export_packet([_cluster()], source="book.docx")
    assert pkt["packet_schema_version"] == 1
    assert pkt["source"] == "book.docx"
    (c,) = pkt["clusters"]
    assert c["cluster_id"] == "cl-0001"
    assert c["original"] == "very unique"
    assert c["intent_zone"] is False
    assert c["options"][0]["model"] == "gpt-5.6-luna"
    assert c["decision"]["action"] is None       # unruled template


def test_import_accept_produces_one_edit_finding():
    pkt = export_packet([_cluster()])
    pkt["clusters"][0]["decision"] = {"action": "accept", "chosen_index": 0,
                                      "confidence": "high", "rationale": "yes"}
    res = import_decisions(pkt)
    assert len(res.findings) == 1
    f = res.findings[0]
    assert f.force_query is False
    assert "unique" in f.corrected_text and "very unique" not in f.corrected_text
    assert res.counts["accept"] == 1


def test_import_query_produces_a_query_channel_finding():
    pkt = export_packet([_cluster()])
    pkt["clusters"][0]["decision"] = {"action": "query", "confidence": "medium"}
    res = import_decisions(pkt)
    assert res.findings[0].force_query is True
    assert res.counts["query"] == 1


def test_import_replace_uses_the_judges_own_text():
    pkt = export_packet([_cluster()])
    pkt["clusters"][0]["decision"] = {"action": "replace",
                                      "replacement": "singular",
                                      "confidence": "medium"}
    res = import_decisions(pkt)
    assert "singular" in res.findings[0].corrected_text


def test_import_reject_and_unruled_produce_no_finding():
    pkt = export_packet([_cluster(), _cluster()])
    pkt["clusters"][0]["decision"] = {"action": "reject"}
    # second stays unruled (action None)
    res = import_decisions(pkt)
    assert res.findings == []
    assert res.counts["reject"] == 1
    assert res.counts["unruled"] == 1


def test_empty_packet_imports_to_zero_findings_not_an_error():
    res = import_decisions({"clusters": [], "lane": "copyedit"})
    assert res.findings == []


# --- the four refusals -------------------------------------------------------

def test_refuses_a_decision_whose_original_no_longer_anchors():
    pkt = export_packet([_cluster()])
    pkt["clusters"][0]["para_text"] = "A completely different paragraph now."
    pkt["clusters"][0]["decision"] = {"action": "accept", "chosen_index": 0}
    with pytest.raises(PacketError, match="anchor"):
        import_decisions(pkt)


def test_refuses_a_duplicate_cluster_id_atomicity():
    pkt = export_packet([_cluster()])
    dup = copy.deepcopy(pkt["clusters"][0])
    dup["decision"] = {"action": "reject"}
    pkt["clusters"][0]["decision"] = {"action": "accept", "chosen_index": 0}
    pkt["clusters"].append(dup)
    with pytest.raises(PacketError, match="atomicity"):
        import_decisions(pkt)


def test_refuses_an_unknown_action_channel():
    pkt = export_packet([_cluster()])
    pkt["clusters"][0]["decision"] = {"action": "rewrite_everything"}
    with pytest.raises(PacketError, match="unknown action"):
        import_decisions(pkt)


def test_refuses_an_edit_inside_an_intent_zone():
    pkt = export_packet([_cluster()],
                        intent_zones={"body-0001": [(0, 30)]})
    assert pkt["clusters"][0]["intent_zone"] is True
    pkt["clusters"][0]["decision"] = {"action": "accept", "chosen_index": 0}
    with pytest.raises(PacketError, match="intent.zone"):
        import_decisions(pkt)
    # but a query on the same protected span is allowed
    pkt["clusters"][0]["decision"] = {"action": "query"}
    res = import_decisions(pkt)
    assert res.findings[0].force_query is True


def test_intent_zone_flag_is_only_set_when_the_span_intersects():
    # zone that does not cover [7:18]
    pkt = export_packet([_cluster()], intent_zones={"body-0001": [(19, 25)]})
    assert pkt["clusters"][0]["intent_zone"] is False


def test_accept_with_bad_index_is_refused():
    pkt = export_packet([_cluster()])
    pkt["clusters"][0]["decision"] = {"action": "accept", "chosen_index": 9}
    with pytest.raises(PacketError, match="chosen_index"):
        import_decisions(pkt)


def test_actions_constant_is_the_documented_set():
    assert set(ACTIONS) == {"accept", "replace", "query", "reject"}
