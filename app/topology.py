"""Build the physical A/B-labelled port graph from a validated manifest."""

from __future__ import annotations

from .models import (
    BridgeVertex,
    Edge,
    Graph,
    Leg,
    Manifest,
    NodeInfo,
    PhysicalPort,
    TerminalBinding,
    Vertex,
)


class TopologyBuildError(ValueError):
    """Raised when structural references cannot be turned into a graph."""


def build_graph(manifest: Manifest) -> Graph:
    """Create physical port vertices, conductors, splices and named bridge stars."""

    vertices: dict[Vertex, list[Edge]] = {}
    edges: set[Edge] = set()
    nodes: dict[str, NodeInfo] = {}
    terminal_by_port: dict[PhysicalPort, TerminalBinding] = {}
    local_legs: dict[tuple[str, str], Leg] = {}

    def vertex(node: str, port: str) -> PhysicalPort:
        v = PhysicalPort(node=node, port=port)
        vertices.setdefault(v, [])
        return v

    def add(edge: Edge) -> None:
        if edge in edges:
            raise TopologyBuildError(f"duplicate graph edge {edge.id}")
        edges.add(edge)
        vertices.setdefault(edge.u, []).append(edge)
        vertices.setdefault(edge.v, []).append(edge)

    # Local A/B labels come from the cable pair connected at each physical joint/cap/terminal port.
    for segment in manifest.segments:
        for pair in segment.pairs:
            for cable_leg, leg in ((pair.a, "A"), (pair.b, "B")):
                first, second = cable_leg.endpoints
                for endpoint in (first, second):
                    key = (endpoint.node, endpoint.port)
                    previous = local_legs.get(key)
                    if previous is not None and previous != leg:
                        # Schema rejects multiple conductors on a port; this remains a hard guard.
                        raise TopologyBuildError(
                            f"port {endpoint.node}/{endpoint.port} is attached to both A and B conductors"
                        )
                    local_legs[key] = leg

    for route in manifest.routes:
        for endpoint, leg in ((route.office.a, "A"), (route.office.b, "B")):
            vertex(endpoint.node, endpoint.port)
            p = PhysicalPort(endpoint.node, endpoint.port)
            terminal_by_port[p] = TerminalBinding(route.id, "office", leg)
        for endpoint, leg in ((route.subscriber.a, "A"), (route.subscriber.b, "B")):
            vertex(endpoint.node, endpoint.port)
            p = PhysicalPort(endpoint.node, endpoint.port)
            terminal_by_port[p] = TerminalBinding(route.id, "subscriber", leg)

    for cap in manifest.caps:
        for endpoint, leg in ((cap.a, "A"), (cap.b, "B")):
            vertex(endpoint.node, endpoint.port)
            p = PhysicalPort(endpoint.node, endpoint.port)
            terminal_by_port[p] = TerminalBinding(cap.id, "cap", leg)

    for open_end in manifest.open_ends:
        for port in open_end.ports:
            vertex(open_end.node, port)

    for joint in manifest.joints:
        for splice in joint.splices:
            vertex(joint.id, splice.a[1])
            vertex(joint.id, splice.b[1])
        for bridge in joint.test_bridges:
            for port in (*bridge.a_ports, *bridge.b_ports):
                vertex(joint.id, port)
            for leg, ports in (("A", bridge.a_ports), ("B", bridge.b_ports)):
                bv = BridgeVertex(bridge_id=bridge.id, joint=joint.id, leg=leg)
                vertices.setdefault(bv, [])
                for port in ports:
                    add(
                        Edge(
                            id=f"bridge:{bridge.id}:{leg}:{port}",
                            type="bridge",
                            u=PhysicalPort(joint.id, port),
                            v=bv,
                            joint=joint.id,
                            bridge=bridge.id,
                        )
                    )

    # Cable conductors connect same physical pair designation A-A and B-B. Local leg labels at
    # joints therefore preserve the parity label until a splice explicitly swaps it.
    edge_index = 0
    for segment in manifest.segments:
        for pair in segment.pairs:
            for cable_leg, leg in ((pair.a, "A"), (pair.b, "B")):
                first, second = cable_leg.endpoints
                edge_index += 1
                add(
                    Edge(
                        id=f"conductor:{edge_index:04d}:{segment.id}:{pair.pair}:{leg}",
                        type="conductor",
                        u=PhysicalPort(first.node, first.port),
                        v=PhysicalPort(second.node, second.port),
                        segment=segment.id,
                        pair=pair.pair,
                    )
                )

    for joint in manifest.joints:
        for splice in joint.splices:
            a_leg = local_legs.get((joint.id, splice.a[1]))
            b_leg = local_legs.get((joint.id, splice.b[1]))
            if a_leg is None or b_leg is None:
                raise TopologyBuildError(
                    f"splice at joint {joint.id} references a port without cable conductor"
                )
            add(
                Edge(
                    id=f"splice:{joint.id}:{splice.index}",
                    type="splice",
                    u=PhysicalPort(joint.id, splice.a[1]),
                    v=PhysicalPort(joint.id, splice.b[1]),
                    joint=joint.id,
                )
            )

    non_joint_nodes = {
        endpoint.node
        for route in manifest.routes
        for endpoint in (route.office.a, route.office.b, route.subscriber.a, route.subscriber.b)
    }
    non_joint_nodes.update(endpoint.node for cap in manifest.caps for endpoint in (cap.a, cap.b))
    non_joint_nodes.update(open_end.node for open_end in manifest.open_ends)

    # Build role/leg lookup after all physical ports are known.
    for vertex_key in vertices:
        if not isinstance(vertex_key, PhysicalPort) or vertex_key.node in non_joint_nodes:
            continue
        leg = local_legs.get((vertex_key.node, vertex_key.port))
        if leg is not None:
            info = nodes.setdefault(vertex_key.node, NodeInfo(role="joint", leg_by_port={}))
            info.leg_by_port[vertex_key.port] = leg

    for route in manifest.routes:
        _set_terminal_node(nodes, route.office.a.node, "office", route.id, local_legs)
        _set_terminal_node(nodes, route.subscriber.a.node, "subscriber", route.id, local_legs)
        for endpoint, expected in (
            (route.office.a, "A"),
            (route.office.b, "B"),
            (route.subscriber.a, "A"),
            (route.subscriber.b, "B"),
        ):
            actual = local_legs.get((endpoint.node, endpoint.port))
            if actual is not None and actual != expected:
                # Schema guarantees route terminals have conductors; this protects the invariant.
                raise TopologyBuildError(
                    f"terminal {endpoint.node}/{endpoint.port} is labelled {actual}, expected {expected}"
                )

    for cap in manifest.caps:
        _set_cap_node(nodes, cap.a.node, cap.id, local_legs)
        for endpoint, expected in ((cap.a, "A"), (cap.b, "B")):
            actual = local_legs.get((endpoint.node, endpoint.port))
            if actual is not None and actual != expected:
                raise TopologyBuildError(
                    f"cap terminal {endpoint.node}/{endpoint.port} is labelled {actual}, expected {expected}"
                )

    for open_end in manifest.open_ends:
        _set_simple_node(nodes, open_end.node, "open_end", local_legs)

    _sort_adjacency(vertices)
    return Graph(
        vertices=vertices,
        edges=edges,
        nodes=nodes,
        terminal_by_port=terminal_by_port,
        manifest=manifest,
    )


def _set_terminal_node(
    nodes: dict[str, NodeInfo],
    node: str,
    role: str,
    route_id: str,
    local_legs: dict[tuple[str, str], Leg],
) -> None:
    existing = nodes.get(node)
    leg_by_port = {
        port: leg for (n, port), leg in local_legs.items() if n == node
    }
    if existing is not None:
        if existing.role != role:
            raise TopologyBuildError(f"node {node} has conflicting terminal role")
        existing.leg_by_port.update(leg_by_port)
    else:
        nodes[node] = NodeInfo(role=role, leg_by_port=leg_by_port, route_id=route_id)  # type: ignore[arg-type]


def _set_simple_node(
    nodes: dict[str, NodeInfo],
    node: str,
    role: str,
    local_legs: dict[tuple[str, str], Leg],
) -> None:
    leg_by_port = {port: leg for (n, port), leg in local_legs.items() if n == node}
    existing = nodes.get(node)
    if existing is not None:
        if existing.role != role:
            raise TopologyBuildError(f"node {node} has conflicting role")
        existing.leg_by_port.update(leg_by_port)
    else:
        nodes[node] = NodeInfo(role=role, leg_by_port=leg_by_port)  # type: ignore[arg-type]


def _set_cap_node(
    nodes: dict[str, NodeInfo], node: str, cap_id: str, local_legs: dict[tuple[str, str], Leg]
) -> None:
    existing = nodes.get(node)
    leg_by_port = {port: leg for (n, port), leg in local_legs.items() if n == node}
    if existing is not None:
        if existing.role != "cap" or existing.cap_id != cap_id:
            raise TopologyBuildError(f"node {node} has conflicting cap role")
        existing.leg_by_port.update(leg_by_port)
    else:
        nodes[node] = NodeInfo(role="cap", leg_by_port=leg_by_port, cap_id=cap_id)


def _sort_adjacency(vertices: dict[Vertex, list[Edge]]) -> None:
    for v, adj in vertices.items():
        adj.sort(key=lambda edge: (edge.type, edge.id))
