"""End-to-end topology proof tests.

Fixtures are explicit test topologies only; the application itself contains no hard-coded
splicing examples.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from fastapi.testclient import TestClient

from app.http_report import app
from app.parser import parse_yaml
from app.proof import analyze_graph
from app.topology import build_graph


client = TestClient(app)


def analyze_text(yaml_text: str):
    manifest = parse_yaml(textwrap.dedent(yaml_text).strip())
    return analyze_graph(build_graph(manifest))


def codes(report):
    return sorted(fault["code"] for fault in report["faults"])


STRAIGHT_TEMPLATE = """
version: 1
routes:
  - id: R1
    expected_polarity: {polarity}
    office:
      a: {{node: OFFICE, port: OA}}
      b: {{node: OFFICE, port: OB}}
    subscriber:
      a: {{node: SUB, port: SA}}
      b: {{node: SUB, port: SB}}
segments:
  - id: S1
    pairs:
      - pair: P1
        a:
          endpoints: [{{node: OFFICE, port: OA}}, {{node: J1, port: s1a}}]
        b:
          endpoints: [{{node: OFFICE, port: OB}}, {{node: J1, port: s1b}}]
  - id: S2
    pairs:
      - pair: P2
        a:
          endpoints: [{{node: J1, port: s2a}}, {{node: J2, port: s2a}}]
        b:
          endpoints: [{{node: J1, port: s2b}}, {{node: J2, port: s2b}}]
  - id: S3
    pairs:
      - pair: P3
        a:
          endpoints: [{{node: J2, port: s3a}}, {{node: SUB, port: SA}}]
        b:
          endpoints: [{{node: J2, port: s3b}}, {{node: SUB, port: SB}}]
joints:
  - id: J1
    splices: {j1}
  - id: J2
    splices: {j2}
caps: []
open_ends: []
"""

SPLICE_STRAIGHT = """
      - a: {node: J1, port: PLACEA}
        b: {node: J1, port: PLACEB}
      - a: {node: J1, port: PLACEA2}
        b: {node: J1, port: PLACEB2}
"""


def splice(joint, mapping_a, mapping_b):
    # mapping tuples describe physical local A and B ports joined together.
    return textwrap.dedent(f"""
          - a: {{node: {joint}, port: {mapping_a[0]}}}
            b: {{node: {joint}, port: {mapping_a[1]}}}
          - a: {{node: {joint}, port: {mapping_b[0]}}}
            b: {{node: {joint}, port: {mapping_b[1]}}}
    """).strip()


def chain_manifest(j1_ports=(("s1a", "s2a"), ("s1b", "s2b")),
                   j2_ports=(("s2a", "s3a"), ("s2b", "s3b")),
                   polarity="normal"):
    j1 = "[" + splice("J1", *j1_ports) + "]"
    j2 = "[" + splice("J2", *j2_ports) + "]"
    # YAML block lists cannot be inline after scalar using this interpolation, so use flow mappings.
    j1_flow = (
        f"[{{a: {{node: J1, port: {j1_ports[0][0]}}}, b: {{node: J1, port: {j1_ports[0][1]}}}}},"
        f" {{a: {{node: J1, port: {j1_ports[1][0]}}}, b: {{node: J1, port: {j1_ports[1][1]}}}}}]"
    )
    j2_flow = (
        f"[{{a: {{node: J2, port: {j2_ports[0][0]}}}, b: {{node: J2, port: {j2_ports[0][1]}}}}},"
        f" {{a: {{node: J2, port: {j2_ports[1][0]}}}, b: {{node: J2, port: {j2_ports[1][1]}}}}}]"
    )
    return STRAIGHT_TEMPLATE.format(polarity=polarity, j1=j1_flow, j2=j2_flow)


def test_legal_straight_route_is_end_to_end_proved():
    report = analyze_text(chain_manifest())
    assert report["status"] == "proved"
    assert report["serviceable"] is True
    assert report["faults"] == []
    route = report["routes"][0]
    assert route["cumulative_polarity"] == "normal"
    assert route["swap_count"] == {"a": 0, "b": 0}
    assert [p["pair"] for p in route["path_a"]["cable_pairs"]] == ["P1", "P2", "P3"]
    assert [s["joint"] for s in route["path_a"]["joint_stages"]] == ["J1", "J2"]


def test_double_swap_is_proved_as_reversed_and_not_pair_split():
    swapped_j1 = (("s1a", "s2b"), ("s1b", "s2a"))
    swapped_j2 = (("s2a", "s3b"), ("s2b", "s3a"))
    report = analyze_text(chain_manifest(swapped_j1, swapped_j2, polarity="normal"))
    assert report["status"] == "proved"
    assert report["faults"] == []
    route = report["routes"][0]
    assert route["cumulative_polarity"] == "normal"
    assert route["swap_count"] == {"a": 2, "b": 2}
    # The audit path names both physical parity changes.
    assert all(s["parity"] == "swap" for s in route["path_a"]["joint_stages"])


def test_single_ab_reversal_is_failure_evidence_not_serviceable():
    swapped_j1 = (("s1a", "s2b"), ("s1b", "s2a"))
    report = analyze_text(chain_manifest(swapped_j1))
    assert report["status"] == "failed"
    assert report["serviceable"] is False
    assert "wrong_polarity" in codes(report)
    route = report["routes"][0]
    assert route["proved"] is False
    assert route["cumulative_polarity"] == "reversed"
    assert route["path_a"]["joint_stages"][0]["parity"] == "swap"


CROSS_JOINT_MANIFEST = (Path(__file__).parent / "fixtures_cross_joint.yaml").read_text()


BRIDGE_MANIFEST_TEMPLATE = """
version: 1
routes:
  - id: R1
    expected_polarity: normal
    office: {a: {node: OFFICE, port: OA}, b: {node: OFFICE, port: OB}}
    subscriber: {a: {node: SUB, port: SA}, b: {node: SUB, port: SB}}
segments:
  - id: FEED
    pairs:
      - pair: PF
        a: {endpoints: [{node: OFFICE, port: OA}, {node: J1, port: inA}]}
        b: {endpoints: [{node: OFFICE, port: OB}, {node: J1, port: inB}]}
  - id: THROUGH
    pairs:
      - pair: PT
        a: {endpoints: [{node: J1, port: outA}, {node: SUB, port: SA}]}
        b: {endpoints: [{node: J1, port: outB}, {node: SUB, port: SB}]}
  - id: BYPASS
    pairs:
      - pair: PB
        a: {endpoints: [{node: J1, port: tapA}, {node: TIP, port: capA}]}
        b: {endpoints: [{node: J1, port: tapB}, {node: TIP, port: capB}]}
joints:
  - id: J1
    splices: []
    test_bridges:
      - id: TB1
        a_ports: [inA, outA, tapA]
        b_ports: [inB, outB, tapB]
caps: __CAPS__
open_ends: __OPEN_ENDS__
"""

LEGAL_BRIDGE = BRIDGE_MANIFEST_TEMPLATE.replace(
    "__CAPS__",
    "[{id: CAP1, a: {node: TIP, port: capA}, b: {node: TIP, port: capB}}]",
).replace("__OPEN_ENDS__", "[]")
UNCAPPED_BRIDGE = BRIDGE_MANIFEST_TEMPLATE.replace("__CAPS__", "[]").replace(
    "__OPEN_ENDS__", "[{id: OPEN1, node: TIP, ports: [capA, capB]}]"
)


def test_named_test_bridge_with_sealed_pair_bypass_is_allowed():
    report = analyze_text(LEGAL_BRIDGE)
    assert report["status"] == "proved"
    assert report["faults"] == []
    route = report["routes"][0]
    assert len(route["test_bridge_bypasses"]) == 1
    bypass = route["test_bridge_bypasses"][0]
    assert bypass["bridge_id"] == "TB1"
    assert bypass["joint"] == "J1"
    assert bypass["cap_id"] == "CAP1"
    assert bypass["sealed"] is True
    assert bypass["a"]["leaf_port"] == "TIP/capA"
    assert bypass["b"]["leaf_port"] == "TIP/capB"
    assert route["path_a"]["joint_stages"][0]["type"] == "test_bridge"
    assert route["test_bridge_bypasses"][0]["a"]["leaf_port"] == "TIP/capA"


def test_uncapped_test_bridge_bypass_is_failure_with_physical_evidence():
    report = analyze_text(UNCAPPED_BRIDGE)
    assert report["serviceable"] is False
    assert "uncapped_test_bridge_branch" in codes(report)
    evidence = [f for f in report["faults"] if f["code"] == "uncapped_test_bridge_branch"]
    assert {"J1/tapA", "TIP/capA"}.issubset(evidence[0]["ports"])


LOOP_MANIFEST = """
version: 1
routes:
  - id: R0
    expected_polarity: normal
    office: {a: {node: O0, port: oa}, b: {node: O0, port: ob}}
    subscriber: {a: {node: S0, port: sa}, b: {node: S0, port: sb}}
segments:
  - id: STRAIGHT
    pairs:
      - pair: P0
        a: {endpoints: [{node: O0, port: oa}, {node: S0, port: sa}]}
        b: {endpoints: [{node: O0, port: ob}, {node: S0, port: sb}]}
  - id: S1
    pairs:
      - pair: P1
        a: {endpoints: [{node: J1, port: a1}, {node: J2, port: a1}]}
        b: {endpoints: [{node: J1, port: b1}, {node: J2, port: b1}]}
  - id: S2
    pairs:
      - pair: P2
        a: {endpoints: [{node: J1, port: a2}, {node: J2, port: a2}]}
        b: {endpoints: [{node: J1, port: b2}, {node: J2, port: b2}]}
joints:
  - id: J1
    splices:
      - {a: {node: J1, port: a1}, b: {node: J1, port: a2}}
      - {a: {node: J1, port: b1}, b: {node: J1, port: b2}}
  - id: J2
    splices:
      - {a: {node: J2, port: a1}, b: {node: J2, port: a2}}
      - {a: {node: J2, port: b1}, b: {node: J2, port: b2}}
caps: []
open_ends: []
"""


def test_closed_loop_is_reported_as_topology_conflict():
    report = analyze_text(LOOP_MANIFEST)
    assert report["status"] == "failed"
    assert "closed_loop" in codes(report)
    cycle = report["faults"][0]["evidence"]
    assert cycle["kind"] == "closed_loop"
    assert cycle["edges"]
    assert cycle["vertices"][0] == cycle["vertices"][-1]


def test_strict_unknown_field_is_422_and_topology_conflict_is_200():
    bad = LEGAL_BRIDGE.replace("version: 1\n", "version: 1\nmystery: true\n")
    response = client.post("/prove", content=bad, headers={"Content-Type": "application/yaml"})
    assert response.status_code == 422
    assert response.json()["errors"][0]["code"] == "unknown_field"

    response = client.post("/prove", content=LOOP_MANIFEST, headers={"Content-Type": "application/yaml"})
    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["serviceable"] is False


def test_empty_manifest_is_structural_rejection_not_a_proof():
    empty = """
version: 1
routes: []
segments: []
joints: []
caps: []
"""
    response = client.post(
        "/prove", content=textwrap.dedent(empty), headers={"Content-Type": "application/yaml"}
    )
    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "invalid_manifest"
    assert body["serviceable"] is False
    assert any(error["code"] == "empty_routes" for error in body["errors"])


def test_duplicate_joint_port_across_splice_and_bridge_is_422_not_loop():
    manifest = """
version: 1
routes:
  - id: R1
    expected_polarity: normal
    office: {a: {node: O, port: oa}, b: {node: O, port: ob}}
    subscriber: {a: {node: S, port: sa}, b: {node: S, port: sb}}
segments:
  - id: FEED
    pairs:
      - pair: PF
        a: {endpoints: [{node: O, port: oa}, {node: J, port: pa}]}
        b: {endpoints: [{node: O, port: ob}, {node: J, port: pb}]}
      - pair: PQ
        a: {endpoints: [{node: K1, port: qa}, {node: J, port: qa}]}
        b: {endpoints: [{node: K1, port: qb}, {node: J, port: qb}]}
      - pair: PT
        a: {endpoints: [{node: K2, port: ta}, {node: J, port: ta}]}
        b: {endpoints: [{node: K2, port: tb}, {node: J, port: tb}]}
      - pair: PD
        a: {endpoints: [{node: S, port: sa}, {node: K3, port: da}]}
        b: {endpoints: [{node: S, port: sb}, {node: K3, port: db}]}
joints:
  - id: J
    splices:
      # Physical port pa is simultaneously spliced and claimed by the named bridge below.
      - {a: {node: J, port: pa}, b: {node: J, port: qa}}
    test_bridges:
      - id: TB
        a_ports: [pa, ta, qa]
        b_ports: [pb, tb, qb]
caps: []
open_ends:
  - {id: K, node: K2, ports: [ta, tb]}
  - {id: K1O, node: K1, ports: [qa, qb]}
  - {id: K3O, node: K3, ports: [da, db]}
"""
    response = client.post(
        "/prove", content=textwrap.dedent(manifest), headers={"Content-Type": "application/yaml"}
    )
    assert response.status_code == 422
    codes_ = [error["code"] for error in response.json()["errors"]]
    assert "duplicate_port" in codes_
    assert response.json()["status"] == "invalid_manifest"


CROSSED_CAP_MANIFEST = """
version: 1
routes:
  - id: R1
    expected_polarity: normal
    office: {a: {node: OFFICE, port: OA}, b: {node: OFFICE, port: OB}}
    subscriber: {a: {node: SUB, port: SA}, b: {node: SUB, port: SB}}
segments:
  - id: FEED
    pairs:
      - pair: PF
        a: {endpoints: [{node: OFFICE, port: OA}, {node: J1, port: inA}]}
        b: {endpoints: [{node: OFFICE, port: OB}, {node: J1, port: inB}]}
  - id: THROUGH
    pairs:
      - pair: PT
        a: {endpoints: [{node: J1, port: outA}, {node: SUB, port: SA}]}
        b: {endpoints: [{node: J1, port: outB}, {node: SUB, port: SB}]}
  - id: BY
    pairs:
      - pair: PX
        a: {endpoints: [{node: J1, port: tapA}, {node: J2, port: xA}]}
        b: {endpoints: [{node: J1, port: tapB}, {node: J2, port: xB}]}
  - id: TAIL
    pairs:
      - pair: PY
        a: {endpoints: [{node: J2, port: yA}, {node: TIP, port: capA}]}
        b: {endpoints: [{node: J2, port: yB}, {node: TIP, port: capB}]}
joints:
  - id: J1
    splices: []
    test_bridges:
      - id: TB1
        a_ports: [inA, outA, tapA]
        b_ports: [inB, outB, tapB]
  - id: J2
    splices:
      # One A/B swap in the sealed bypass crosses the cap legs.
      - {a: {node: J2, port: xA}, b: {node: J2, port: yB}}
      - {a: {node: J2, port: xB}, b: {node: J2, port: yA}}
caps: [{id: CAP1, a: {node: TIP, port: capA}, b: {node: TIP, port: capB}}]
open_ends: []
"""


def test_crossed_bridge_bypass_at_cap_is_failure_evidence():
    report = analyze_text(CROSSED_CAP_MANIFEST)
    assert report["status"] == "failed"
    assert report["serviceable"] is False
    assert "test_bridge_bypass_reversed" in codes(report)
    route = report["routes"][0]
    assert route["proved"] is False
    assert route["test_bridge_bypasses"][0]["sealed"] is False


def test_malformed_content_returns_structural_422_not_500():
    nasty_cases = [
        "version: 1\nroutes: null\nsegments: null\njoints: null\ncaps: null\n",
        "version: 1\nroutes: 42\nsegments: 7\njoints: {}\ncaps: []\n",
        "version: 1\nroutes: [{id: [1]}]\nsegments: []\njoints: []\ncaps: []\n",
        "version: 1\nroutes: []\nsegments: [{id: s, pairs: 9}]\njoints: []\ncaps: []\n",
        "&a {x: *a}\nversion: 1\n",
    ]
    for body in nasty_cases:
        response = client.post(
            "/prove", content=body, headers={"Content-Type": "application/yaml"}
        )
        assert response.status_code == 422, body
        assert response.json()["status"] == "invalid_manifest"
        assert response.json()["serviceable"] is False


def test_requires_yaml_content_type():
    response = client.post("/prove", content="version: 1\n", headers={"Content-Type": "application/json"})
    assert response.status_code == 415
