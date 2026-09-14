"""Domain models shared by parsing, graph construction and proof generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

NodeRole = Literal["joint", "office", "subscriber", "cap", "open_end"]
Leg = Literal["A", "B"]
Polarity = Literal["normal", "reversed"]
SplicePort = tuple[str, str]


@dataclass(frozen=True)
class Endpoint:
    node: str
    port: str


@dataclass(frozen=True)
class CableLeg:
    endpoints: tuple[Endpoint, Endpoint]


@dataclass(frozen=True)
class PairSpec:
    pair: str
    a: CableLeg
    b: CableLeg


@dataclass(frozen=True)
class TerminalPair:
    a: Endpoint
    b: Endpoint


@dataclass(frozen=True)
class SegmentSpec:
    id: str
    pairs: tuple[PairSpec, ...]


@dataclass(frozen=True)
class SpliceSpec:
    index: int
    a: SplicePort
    b: SplicePort


@dataclass(frozen=True)
class JointSpec:
    id: str
    splices: tuple[SpliceSpec, ...] = field(default_factory=tuple)
    test_bridges: tuple["TestBridgeSpec", ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TestBridgeSpec:
    id: str
    joint: str
    a_ports: tuple[str, ...]
    b_ports: tuple[str, ...]


@dataclass(frozen=True)
class RouteSpec:
    id: str
    expected_polarity: Polarity
    office: TerminalPair
    subscriber: TerminalPair


@dataclass(frozen=True)
class CapSpec:
    id: str
    a: Endpoint
    b: Endpoint


@dataclass(frozen=True)
class OpenEndSpec:
    id: str
    node: str
    ports: tuple[str, ...]


@dataclass(frozen=True)
class Manifest:
    version: int
    routes: tuple[RouteSpec, ...]
    segments: tuple[SegmentSpec, ...]
    joints: tuple[JointSpec, ...]
    caps: tuple[CapSpec, ...] = field(default_factory=tuple)
    open_ends: tuple[OpenEndSpec, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    location: str = ""


class ManifestError(ValueError):
    """A collection of strict manifest/schema validation failures."""

    def __init__(self, issues: list[ValidationIssue]):
        self.issues = issues
        super().__init__("; ".join(issue.message for issue in issues[:5]))


# ---- Graph models ---------------------------------------------------------


class VertexKind(str, Enum):
    PHYSICAL = "physical"
    BRIDGE = "test_bridge"


@dataclass(frozen=True)
class PhysicalPort:
    node: str
    port: str

    def __str__(self) -> str:
        return f"{self.node}/{self.port}"


@dataclass(frozen=True)
class BridgeVertex:
    bridge_id: str
    joint: str
    leg: Leg

    def __str__(self) -> str:
        return f"{self.bridge_id}:{self.leg}"


Vertex = PhysicalPort | BridgeVertex


@dataclass(frozen=True)
class Edge:
    id: str
    type: Literal["conductor", "splice", "bridge"]
    u: Vertex
    v: Vertex
    segment: str | None = None
    pair: str | None = None
    joint: str | None = None
    bridge: str | None = None


TerminalKind = Literal["office", "subscriber", "cap"]


@dataclass(frozen=True)
class TerminalBinding:
    owner: str
    kind: TerminalKind
    expected_leg: Leg


@dataclass(frozen=True)
class NodeInfo:
    role: NodeRole
    leg_by_port: dict[str, Leg]
    cap_id: str | None = None
    route_id: str | None = None


@dataclass
class Graph:
    vertices: dict[Vertex, list[Edge]]
    edges: set[Edge]
    nodes: dict[str, NodeInfo]
    terminal_by_port: dict[PhysicalPort, TerminalBinding]
    manifest: Manifest
