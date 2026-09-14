"""Connected-component proof over the A/B-labelled physical port graph."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

from .models import (
    BridgeVertex,
    Edge,
    Graph,
    Leg,
    PhysicalPort,
    RouteSpec,
    TerminalBinding,
    Vertex,
)


@dataclass
class Component:
    cid: str
    vertices: list[Vertex]
    vertex_set: set[Vertex]
    cycles: list[dict[str, Any]]
    terminals: list[tuple[PhysicalPort, TerminalBinding]]
    offices: list[tuple[PhysicalPort, TerminalBinding]]
    subscribers: list[tuple[PhysicalPort, TerminalBinding]]
    caps: list[tuple[PhysicalPort, TerminalBinding]]
    has_fault: bool = False


def vertex_id(v: Vertex) -> str:
    return str(v)


def edge_dict(edge: Edge, reverse: bool = False) -> dict[str, Any]:
    u, v = (edge.v, edge.u) if reverse else (edge.u, edge.v)
    item: dict[str, Any] = {
        "id": edge.id,
        "type": edge.type,
        "from": vertex_id(u),
        "to": vertex_id(v),
    }
    if edge.segment:
        item["segment"] = edge.segment
    if edge.pair:
        item["pair"] = edge.pair
    if edge.joint:
        item["joint"] = edge.joint
    if edge.bridge:
        item["bridge"] = edge.bridge
    return item


def analyze_graph(graph: Graph) -> dict[str, Any]:
    """Prove or refute every route using complete connected components, not local splices."""

    faults = _Faults()
    components = _components(graph, faults)
    component_by_vertex = {
        vertex: component for component in components for vertex in component.vertex_set
    }

    _check_bridge_legality(graph, faults)
    for component in components:
        _check_component(graph, component, faults)

    routes_report = [
        _prove_route(graph, route, component_by_vertex, faults)
        for route in graph.manifest.routes
    ]

    global_faults = sorted(
        faults.items,
        key=lambda item: (
            item["ports"],
            item["code"],
            item.get("route_id") or "",
            item.get("component_id") or "",
            item["message"],
        ),
    )
    serviceable = not global_faults and all(route["proved"] for route in routes_report)
    for route in routes_report:
        route["proved"] = serviceable and route["proved"]
        route["status"] = "proved" if route["proved"] else "not_proved"

    components_report = [_component_report(component) for component in components]
    return {
        "status": "proved" if serviceable else "failed",
        "serviceable": serviceable,
        "summary": {
            "route_count": len(graph.manifest.routes),
            "component_count": len(components),
            "proved_route_count": len(graph.manifest.routes) if serviceable else 0,
            "fault_count": len(global_faults),
        },
        "components": components_report,
        "routes": routes_report,
        "faults": global_faults,
    }


class _Faults:
    def __init__(self) -> None:
        self._seen: set[tuple[Any, ...]] = set()
        self.items: list[dict[str, Any]] = []

    def add(
        self,
        code: str,
        message: str,
        ports: list[str] | None = None,
        *,
        route_id: str | None = None,
        bridge_id: str | None = None,
        component_id: str | None = None,
        evidence: dict[str, Any] | list[Any] | None = None,
    ) -> None:
        ports = sorted(ports or [])
        key = (code, tuple(ports), route_id, bridge_id, component_id, _stable(evidence), message)
        if key in self._seen:
            return
        self._seen.add(key)
        item: dict[str, Any] = {
            "code": code,
            "message": message,
            "ports": ports,
        }
        if route_id is not None:
            item["route_id"] = route_id
        if bridge_id is not None:
            item["bridge_id"] = bridge_id
        if component_id is not None:
            item["component_id"] = component_id
        if evidence is not None:
            item["evidence"] = evidence
        self.items.append(item)


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((k, _stable(v)) for k, v in value.items()))
    if isinstance(value, list):
        return tuple(_stable(v) for v in value)
    return value


def _other(edge: Edge, vertex: Vertex) -> Vertex:
    return edge.v if edge.u == vertex else edge.u


def _components(graph: Graph, faults: _Faults) -> list[Component]:
    visited: set[Vertex] = set()
    result: list[Component] = []
    ordered_vertices = sorted(graph.vertices, key=vertex_id)

    for start in ordered_vertices:
        if start in visited:
            continue
        parent: dict[Vertex, Vertex | None] = {start: None}
        parent_edge: dict[Vertex, Edge | None] = {start: None}
        depth = {start: 0}
        stack: list[tuple[Vertex, Any]] = [(start, iter(graph.vertices[start]))]
        comp_vertices: list[Vertex] = []
        back_edges: set[str] = set()

        while stack:
            vertex, adjacency = stack[-1]
            if vertex not in visited:
                visited.add(vertex)
                comp_vertices.append(vertex)
            try:
                edge = next(adjacency)
            except StopIteration:
                stack.pop()
                continue
            nxt = _other(edge, vertex)
            if nxt not in parent:
                parent[nxt] = vertex
                parent_edge[nxt] = edge
                depth[nxt] = depth[vertex] + 1
                stack.append((nxt, iter(graph.vertices[nxt])))
            elif parent_edge[vertex] is not None and edge.id != parent_edge[vertex].id and depth[vertex] > depth[nxt]:
                back_edges.add(edge.id)

        vertex_set = set(comp_vertices)
        cycles: list[dict[str, Any]] = []
        edge_by_id = {edge.id: edge for edge in graph.edges}
        for edge_id in sorted(back_edges):
            edge = edge_by_id[edge_id]
            # In undirected DFS every non-tree edge joins a descendant to an ancestor.
            ancestor, descendant = sorted((edge.u, edge.v), key=lambda v: depth[v])
            path_vertices = [descendant]
            path_edges: list[Edge] = []
            cursor = descendant
            while cursor != ancestor:
                pe = parent_edge[cursor]
                if pe is None:  # pragma: no cover - DFS invariant
                    break
                path_edges.append(pe)
                cursor = parent[cursor]  # type: ignore[assignment]
                path_vertices.append(cursor)
            ancestor_to_descendant = list(reversed(path_vertices))
            cycle_vertices = [*ancestor_to_descendant, ancestor]
            cycle_edges = [*reversed(path_edges), edge]
            cycle = {
                "kind": "closed_loop",
                "vertices": [vertex_id(v) for v in cycle_vertices],
                "edges": [edge_dict(e) for e in cycle_edges],
            }
            cycles.append(cycle)

        terminals = sorted(
            (
                (v, graph.terminal_by_port[v])
                for v in comp_vertices
                if isinstance(v, PhysicalPort) and v in graph.terminal_by_port
            ),
            key=lambda item: vertex_id(item[0]),
        )
        offices = [(v, b) for v, b in terminals if b.kind == "office"]
        subscribers = [(v, b) for v, b in terminals if b.kind == "subscriber"]
        caps = [(v, b) for v, b in terminals if b.kind == "cap"]
        cid = f"C{len(result) + 1:03d}"
        component = Component(
            cid=cid,
            vertices=sorted(comp_vertices, key=vertex_id),
            vertex_set=vertex_set,
            cycles=cycles,
            terminals=terminals,
            offices=offices,
            subscribers=subscribers,
            caps=caps,
        )
        if cycles:
            component.has_fault = True
            physical_ports = sorted(
                (vertex_id(v) for v in component.vertices if isinstance(v, PhysicalPort))
            )
            for cycle in cycles:
                faults.add(
                    "closed_loop",
                    "a connected component contains a closed electrical loop",
                    physical_ports,
                    component_id=cid,
                    evidence=cycle,
                )
        result.append(component)

    return result


def _check_bridge_legality(graph: Graph, faults: _Faults) -> None:
    for joint in graph.manifest.joints:
        for bridge in joint.test_bridges:
            bad_ports: list[str] = []
            for expected, ports in (("A", bridge.a_ports), ("B", bridge.b_ports)):
                for port in ports:
                    leg = graph.nodes[joint.id].leg_by_port.get(port) if joint.id in graph.nodes else None
                    if leg != expected:
                        bad_ports.append(f"{joint.id}/{port}")
            if bad_ports:
                faults.add(
                    "test_bridge_mixed_leg",
                    f"named test bridge {bridge.id} joins physical A and B conductors in one conductive group",
                    bad_ports,
                    bridge_id=bridge.id,
                    evidence={
                        "kind": "bridge_leg_conflict",
                        "joint": joint.id,
                        "a_ports": [f"{joint.id}/{p}" for p in bridge.a_ports],
                        "b_ports": [f"{joint.id}/{p}" for p in bridge.b_ports],
                    },
                )


def _ports(terminals: list[tuple[PhysicalPort, TerminalBinding]]) -> list[str]:
    return sorted(vertex_id(v) for v, _ in terminals)


def _terminal_evidence(terminals: list[tuple[PhysicalPort, TerminalBinding]]) -> list[dict[str, str]]:
    return [
        {"port": vertex_id(v), "owner": binding.owner, "leg": binding.expected_leg, "kind": binding.kind}
        for v, binding in sorted(terminals, key=lambda item: vertex_id(item[0]))
    ]


def _check_component(graph: Graph, component: Component, faults: _Faults) -> None:
    cid = component.cid
    office_routes = {binding.owner for _, binding in component.offices}
    subscriber_routes = {binding.owner for _, binding in component.subscribers}

    if len(component.offices) >= 2:
        if len(office_routes) >= 2:
            code = "multiple_office_feeds"
            message = "one connected component contains feed legs from two different office pairs"
        else:
            code = "office_pair_shorted"
            message = "the A and B legs of one office feed are connected together"
        component.has_fault = True
        faults.add(
            code,
            message,
            _ports(component.offices),
            route_id=next(iter(office_routes)) if len(office_routes) == 1 else None,
            component_id=cid,
            evidence={"kind": code, "terminals": _terminal_evidence(component.offices)},
        )

    if len(component.subscribers) >= 2:
        if len(subscriber_routes) >= 2:
            code = "multiple_subscriber_terminals"
            message = "one connected component reaches subscriber legs belonging to different line pairs"
        else:
            code = "subscriber_pair_shorted"
            message = "the A and B legs at one subscriber pair are connected together"
        component.has_fault = True
        faults.add(
            code,
            message,
            _ports(component.subscribers),
            route_id=next(iter(subscriber_routes)) if len(subscriber_routes) == 1 else None,
            component_id=cid,
            evidence={"kind": code, "terminals": _terminal_evidence(component.subscribers)},
        )

    if not component.offices:
        component.has_fault = True
        if component.subscribers:
            faults.add(
                "component_without_office",
                "a subscriber-connected component has no office feed",
                _ports(component.subscribers),
                component_id=cid,
                evidence={"kind": "component_without_office", "subscribers": _terminal_evidence(component.subscribers)},
            )
        else:
            physical = sorted(
                vertex_id(v) for v in component.vertices if isinstance(v, PhysicalPort)
            )
            faults.add(
                "isolated_component",
                "a cable component is not connected to any office feed; sealed bypasses must branch from a named bridge",
                physical,
                component_id=cid,
                evidence={"kind": "isolated_component", "caps": _terminal_evidence(component.caps)},
            )
    elif len(component.offices) == 1 and not component.subscribers:
        component.has_fault = True
        office, binding = component.offices[0]
        faults.add(
            "subscriber_not_reachable",
            f"office leg {binding.expected_leg} of route {binding.owner} does not reach a subscriber leg",
            [vertex_id(office)],
            route_id=binding.owner,
            component_id=cid,
            evidence={"kind": "dead_feed", "caps": _terminal_evidence(component.caps)},
        )

    if len(component.offices) == 1 and len(component.subscribers) == 1:
        office, ob = component.offices[0]
        subscriber, sb = component.subscribers[0]
        if ob.owner != sb.owner:
            component.has_fault = True
            faults.add(
                "wrong_subscriber_pair",
                f"office route {ob.owner} reaches subscriber route {sb.owner}",
                [vertex_id(office), vertex_id(subscriber)],
                route_id=ob.owner,
                component_id=cid,
                evidence={
                    "kind": "wrong_pair",
                    "office": {"port": vertex_id(office), "route": ob.owner, "leg": ob.expected_leg},
                    "subscriber": {"port": vertex_id(subscriber), "route": sb.owner, "leg": sb.expected_leg},
                },
            )

    if (
        len(component.offices) == 1
        and len(component.subscribers) == 1
        and not component.cycles
    ):
        office, _ = component.offices[0]
        for leaf, binding in [*component.subscribers, *component.caps]:
            found = _bfs(graph, office, leaf)
            if found is None:
                continue
            _, path_edges = found
            if not any(isinstance(edge.u, BridgeVertex) or isinstance(edge.v, BridgeVertex) for edge in path_edges):
                if binding.kind == "cap":
                    component.has_fault = True
                    faults.add(
                        "cap_without_named_test_bridge",
                        f"sealed cap {binding.owner} branches from ordinary degree-two splices, not a named test bridge",
                        [vertex_id(office), vertex_id(leaf)],
                        component_id=cid,
                        evidence={"kind": "illegal_branch", "cap": binding.owner, "leaf": vertex_id(leaf)},
                    )
        # Any non-terminal physical leaf in an office component is an unrecognized open branch.
        for vertex in component.vertices:
            if not isinstance(vertex, PhysicalPort) or vertex in graph.terminal_by_port:
                continue
            if len(graph.vertices[vertex]) == 1:
                found = _bfs(graph, office, vertex)
                traverses_named_bridge = found is not None and any(
                    isinstance(edge.u, BridgeVertex) or isinstance(edge.v, BridgeVertex)
                    for edge in found[1]
                )
                if traverses_named_bridge:
                    continue
                component.has_fault = True
                faults.add(
                    "uncapped_branch",
                    "an office component branches to a physical port that is neither a subscriber nor a declared cap",
                    [vertex_id(office), vertex_id(vertex)],
                    component_id=cid,
                    evidence={"kind": "open_branch", "leaf": vertex_id(vertex)},
                )


def _component_report(component: Component) -> dict[str, Any]:
    return {
        "id": component.cid,
        "status": "invalid" if component.has_fault else "valid",
        "physical_port_count": sum(isinstance(v, PhysicalPort) for v in component.vertices),
        "test_bridge_group_count": sum(isinstance(v, BridgeVertex) for v in component.vertices),
        "office_terminals": _terminal_evidence(component.offices),
        "subscriber_terminals": _terminal_evidence(component.subscribers),
        "cap_terminals": _terminal_evidence(component.caps),
        "cycles": component.cycles,
        "ports": sorted(vertex_id(v) for v in component.vertices if isinstance(v, PhysicalPort)),
    }


def _bfs(graph: Graph, start: Vertex, goal: Vertex) -> tuple[list[Vertex], list[Edge]] | None:
    previous: dict[Vertex, tuple[Vertex, Edge]] = {}
    queue: deque[Vertex] = deque([start])
    seen = {start}
    while queue:
        vertex = queue.popleft()
        if vertex == goal:
            break
        for edge in graph.vertices[vertex]:
            nxt = _other(edge, vertex)
            if nxt not in seen:
                seen.add(nxt)
                previous[nxt] = (vertex, edge)
                queue.append(nxt)
    if goal not in seen:
        return None
    vertices = [goal]
    edges: list[Edge] = []
    cursor = goal
    while cursor != start:
        prev, edge = previous[cursor]
        edges.append(edge)
        vertices.append(prev)
        cursor = prev
    vertices.reverse()
    edges.reverse()
    return vertices, edges


def _trace(graph: Graph, start: PhysicalPort, goal: PhysicalPort) -> dict[str, Any] | None:
    found = _bfs(graph, start, goal)
    if found is None:
        return None
    vertices, edges = found
    hops = []
    cable_pairs = []
    joint_stages = []
    swap_count = 0

    index = 0
    while index < len(edges):
        edge = edges[index]
        reverse = edge.u != vertices[index]
        hops.append(edge_dict(edge, reverse=reverse))
        if edge.type == "conductor":
            cable_pairs.append(
                {
                    "segment": edge.segment,
                    "pair": edge.pair,
                    "from": vertex_id(edge.u if not reverse else edge.v),
                    "to": vertex_id(edge.v if not reverse else edge.u),
                }
            )
        elif edge.type == "splice":
            joint = edge.joint or ""
            from_port = edge.u if not reverse else edge.v
            to_port = edge.v if not reverse else edge.u
            from_leg = graph.nodes[joint].leg_by_port[from_port.port]  # type: ignore[union-attr]
            to_leg = graph.nodes[joint].leg_by_port[to_port.port]  # type: ignore[union-attr]
            swap = from_leg != to_leg
            swap_count += int(swap)
            joint_stages.append(
                {
                    "type": "splice",
                    "joint": joint,
                    "from_port": vertex_id(from_port),
                    "to_port": vertex_id(to_port),
                    "parity": "swap" if swap else "straight",
                }
            )
        elif edge.type == "bridge":
            # Two bridge star edges surround the virtual conductive A or B test-bridge vertex.
            next_edge = edges[index + 1] if index + 1 < len(edges) else None
            if next_edge is None or next_edge.type != "bridge":
                return None
            hops.append(edge_dict(next_edge, reverse=isinstance(next_edge.v, BridgeVertex)))
            entry = edge.v if isinstance(edge.u, BridgeVertex) else edge.u
            exit_ = next_edge.v if isinstance(next_edge.u, BridgeVertex) else next_edge.u
            joint_stages.append(
                {
                    "type": "test_bridge",
                    "joint": edge.joint,
                    "bridge": edge.bridge,
                    "entry_port": vertex_id(entry),
                    "exit_port": vertex_id(exit_),
                    "parity": "straight",
                }
            )
            index += 1
        index += 1

    return {
        "start": vertex_id(start),
        "goal": vertex_id(goal),
        "reachable": True,
        "swap_count": swap_count,
        "cumulative_polarity": "reversed" if swap_count % 2 else "normal",
        "cable_pairs": cable_pairs,
        "joint_stages": joint_stages,
        "hops": hops,
        "vertices": [vertex_id(v) for v in vertices],
    }


def _prove_route(
    graph: Graph,
    route: RouteSpec,
    component_by_vertex: dict[Vertex, Component],
    faults: _Faults,
) -> dict[str, Any]:
    start_a = PhysicalPort(route.office.a.node, route.office.a.port)
    start_b = PhysicalPort(route.office.b.node, route.office.b.port)
    subscriber_a = PhysicalPort(route.subscriber.a.node, route.subscriber.a.port)
    subscriber_b = PhysicalPort(route.subscriber.b.node, route.subscriber.b.port)

    comp_a = component_by_vertex.get(start_a)
    comp_b = component_by_vertex.get(start_b)

    def subscriber_reached(component: Component | None) -> PhysicalPort | None:
        if component is None or len(component.subscribers) != 1:
            return None
        port, binding = component.subscribers[0]
        return port if binding.owner == route.id and not component.has_fault else None

    observed_a = subscriber_reached(comp_a)
    observed_b = subscriber_reached(comp_b)
    if observed_a is not None and observed_b is not None:
        actual_polarity = "normal" if observed_a == subscriber_a else "reversed"
    else:
        actual_polarity = None
    target_a = observed_a or (
        subscriber_b if route.expected_polarity == "reversed" else subscriber_a
    )
    target_b = observed_b or (
        subscriber_a if route.expected_polarity == "reversed" else subscriber_b
    )
    local_faults: list[str] = []

    def fail(code: str, message: str, ports: list[str], evidence: Any | None = None) -> None:
        local_faults.append(code)
        faults.add(
            code,
            message,
            ports,
            route_id=route.id,
            component_id=comp_a.cid if comp_a is not None else None,
            evidence=evidence,
        )

    if comp_a is not None and comp_b is not None and comp_a.cid == comp_b.cid:
        # Global component check emits the precise office-short fault; retain route linkage here too.
        fail(
            "route_pair_split",
            f"route {route.id} office A/B legs are in one component instead of two paired conductors",
            [vertex_id(start_a), vertex_id(start_b)],
        )

    target_comp_a = component_by_vertex.get(target_a)
    target_comp_b = component_by_vertex.get(target_b)
    if target_comp_a is not None and target_comp_b is not None and target_comp_a.cid == target_comp_b.cid:
        fail(
            "route_pair_split",
            f"route {route.id} subscriber A/B legs are shorted into one conductor component",
            [vertex_id(target_a), vertex_id(target_b)],
        )

    trace_a = _trace(graph, start_a, target_a) if comp_a is not None and target_comp_a is comp_a else None
    trace_b = _trace(graph, start_b, target_b) if comp_b is not None and target_comp_b is comp_b else None

    if trace_a is None:
        fail(
            "expected_pair_not_connected",
            f"office A of route {route.id} does not reach expected subscriber pair {route.id}",
            [vertex_id(start_a), vertex_id(subscriber_a)],
        )
    if trace_b is None:
        fail(
            "expected_pair_not_connected",
            f"office B of route {route.id} does not reach expected subscriber pair {route.id}",
            [vertex_id(start_b), vertex_id(subscriber_b)],
        )

    branches: list[dict[str, Any]] = []
    component_clean = (
        comp_a is not None
        and comp_b is not None
        and comp_a is not comp_b
        and not comp_a.has_fault
        and not comp_b.has_fault
    )

    if trace_a is not None and trace_b is not None:
        if trace_a["swap_count"] % 2 != trace_b["swap_count"] % 2:
            fail(
                "asymmetric_polarity",
                f"route {route.id} A and B conductors accumulate different A/B swap parity",
                [vertex_id(start_a), vertex_id(start_b)],
                {"a_swaps": trace_a["swap_count"], "b_swaps": trace_b["swap_count"]},
            )
        actual = actual_polarity or trace_a["cumulative_polarity"]
        if actual != route.expected_polarity:
            fail(
                "wrong_polarity",
                f"route {route.id} is {actual}; expected {route.expected_polarity}",
                [vertex_id(start_a), vertex_id(subscriber_a)],
                {
                    "expected": route.expected_polarity,
                    "actual": actual,
                    "a_swaps": trace_a["swap_count"],
                    "b_swaps": trace_b["swap_count"],
                },
            )
        pairs_a = [(p["segment"], p["pair"]) for p in trace_a["cable_pairs"]]
        pairs_b = [(p["segment"], p["pair"]) for p in trace_b["cable_pairs"]]
        if pairs_a != pairs_b:
            fail(
                "pair_split",
                f"route {route.id} A and B paths do not remain on the same cable pairs through every joint",
                [vertex_id(start_a), vertex_id(start_b)],
                {"a_pairs": pairs_a, "b_pairs": pairs_b},
            )
        stages_a = [(s["type"], s.get("joint"), s.get("bridge")) for s in trace_a["joint_stages"]]
        stages_b = [(s["type"], s.get("joint"), s.get("bridge")) for s in trace_b["joint_stages"]]
        if stages_a != stages_b:
            fail(
                "pair_split_at_joint",
                f"route {route.id} A/B legs do not traverse the same joints and named bridges",
                [vertex_id(start_a), vertex_id(start_b)],
                {"a_joints": stages_a, "b_joints": stages_b},
            )
        if component_clean:
            branches, branch_faults = _prove_branches(graph, route, trace_a, trace_b)
            for code, message, ports, evidence in branch_faults:
                fail(code, message, ports, evidence)

    proved = not local_faults and trace_a is not None and trace_b is not None
    return {
        "route_id": route.id,
        "status": "proved" if proved else "not_proved",
        "proved": proved,
        "expected_polarity": route.expected_polarity,
        "cumulative_polarity": trace_a["cumulative_polarity"] if trace_a else None,
        "swap_count": {
            "a": trace_a["swap_count"] if trace_a else None,
            "b": trace_b["swap_count"] if trace_b else None,
        },
        "office": {"a": vertex_id(start_a), "b": vertex_id(start_b)},
        "subscriber": {"a": vertex_id(subscriber_a), "b": vertex_id(subscriber_b)},
        "path_a": trace_a,
        "path_b": trace_b,
        "test_bridge_bypasses": branches,
    }


def _trace_branch(
    graph: Graph, bridge_vertex: BridgeVertex, entry: PhysicalPort, main_exit: PhysicalPort
) -> dict[str, Any] | None:
    # Find the physical leaf reached from the bridge branch port without returning to the star.
    start: PhysicalPort = entry
    previous: dict[Vertex, tuple[Vertex, Edge]] = {}
    queue: deque[Vertex] = deque([start])
    seen = {start}
    leaf: Vertex | None = None
    while queue:
        vertex = queue.popleft()
        neighbors = [
            (_other(e, vertex), e)
            for e in graph.vertices[vertex]
            if not (vertex == start and _other(e, vertex) == bridge_vertex)
        ]
        if vertex != start and len(neighbors) == 1:
            leaf = vertex
            break
        for nxt, edge in neighbors:
            if nxt is bridge_vertex:
                continue
            if nxt not in seen:
                seen.add(nxt)
                previous[nxt] = (vertex, edge)
                queue.append(nxt)
    if leaf is None or not isinstance(leaf, PhysicalPort):
        return None
    path_vertices = [leaf]
    path_edges: list[Edge] = []
    cursor: Vertex = leaf
    while cursor != start:
        prev, edge = previous[cursor]
        path_edges.append(edge)
        cursor = prev
        path_vertices.append(cursor)
    path_vertices.reverse()
    path_edges.reverse()
    cable_pairs = []
    swap_count = 0
    for from_vertex, edge in zip(path_vertices, path_edges):
        if edge.type == "conductor":
            cable_pairs.append({"segment": edge.segment, "pair": edge.pair})
        elif edge.type == "splice":
            joint = edge.joint or ""
            reverse = edge.u != from_vertex
            from_port = edge.u if not reverse else edge.v
            to_port = edge.v if not reverse else edge.u
            from_leg = graph.nodes[joint].leg_by_port[from_port.port]
            to_leg = graph.nodes[joint].leg_by_port[to_port.port]
            swap_count += int(from_leg != to_leg)
    binding = graph.terminal_by_port.get(leaf)
    return {
        "entry_port": vertex_id(entry),
        "leaf_port": vertex_id(leaf),
        "terminal_kind": binding.kind if binding else None,
        "cap_id": binding.owner if binding and binding.kind == "cap" else None,
        "cap_leg": binding.expected_leg if binding and binding.kind == "cap" else None,
        "swap_count": swap_count,
        "cumulative_polarity": "reversed" if swap_count % 2 else "normal",
        "cable_pairs": cable_pairs,
        "vertices": [vertex_id(v) for v in path_vertices],
    }


def _bridge_exits(trace: dict[str, Any], leg: Leg) -> dict[str, tuple[BridgeVertex, str, str]]:
    result: dict[str, tuple[BridgeVertex, str, str]] = {}
    for stage in trace["joint_stages"]:
        if stage["type"] == "test_bridge":
            result[stage["bridge"]] = (  # type: ignore[assignment]
                BridgeVertex(stage["bridge"], stage["joint"], leg),
                stage["entry_port"],
                stage["exit_port"],
            )
    return result


def _physical_port_for_bridge(
    graph: Graph,
    bridge_vertex: BridgeVertex,
    rendered_port: str,
) -> PhysicalPort:
    """Resolve a rendered physical port that is known to attach to *bridge_vertex*.

    Matching is done by the rendered ``node/port`` form instead of splitting it back apart:
    field node names may themselves contain ``/``, which a naive ``split("/", 1)`` would corrupt.
    """

    for edge in graph.vertices[bridge_vertex]:
        candidate = edge.u if isinstance(edge.u, PhysicalPort) else edge.v
        if vertex_id(candidate) == rendered_port:
            return candidate
    # The bridge trace only names physical ports attached to this star, so this is an invariant.
    raise KeyError(rendered_port)


def _bridge_physical_exits(
    graph: Graph,
    bridge_a: BridgeVertex,
    bridge_b: BridgeVertex,
    port_a: str,
    port_b: str,
) -> tuple[PhysicalPort, PhysicalPort]:
    return (
        _physical_port_for_bridge(graph, bridge_a, port_a),
        _physical_port_for_bridge(graph, bridge_b, port_b),
    )


def _prove_branches(
    graph: Graph, route: RouteSpec, trace_a: dict[str, Any], trace_b: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[tuple[str, str, list[str], dict[str, Any] | None]]]:
    faults: list[tuple[str, str, list[str], dict[str, Any] | None]] = []
    reports: list[dict[str, Any]] = []
    bridges_a = _bridge_exits(trace_a, "A")
    bridges_b = _bridge_exits(trace_b, "B")

    for bridge_id in sorted(set(bridges_a) | set(bridges_b)):
        if bridge_id not in bridges_a or bridge_id not in bridges_b:
            faults.append((
                "test_bridge_pair_mismatch",
                f"test bridge {bridge_id} is traversed by only one leg of route {route.id}",
                [],
                {"bridge": bridge_id},
            ))
            continue
        bva, entry_a, exit_a = bridges_a[bridge_id]
        bvb, entry_b, exit_b = bridges_b[bridge_id]
        pa, pb = _bridge_physical_exits(graph, bva, bvb, entry_a, entry_b)
        pexit_a, pexit_b = _bridge_physical_exits(graph, bva, bvb, exit_a, exit_b)

        branches_a = _all_branch_traces(graph, bva, pa, pexit_a)
        branches_b = _all_branch_traces(graph, bvb, pb, pexit_b)
        by_cap_a = {b["cap_id"]: b for b in branches_a if b["cap_id"]}
        by_cap_b = {b["cap_id"]: b for b in branches_b if b["cap_id"]}

        for branch in (*branches_a, *branches_b):
            if branch["terminal_kind"] != "cap":
                faults.append((
                    "uncapped_test_bridge_branch",
                    f"test bridge {bridge_id} bypass ends at {branch['leaf_port']} instead of a declared cap",
                    [branch["entry_port"], branch["leaf_port"]],
                    {"bridge": bridge_id, "branch": branch},
                ))
        for cap_id in sorted(set(by_cap_a) ^ set(by_cap_b)):
            branch = by_cap_a.get(cap_id) or by_cap_b.get(cap_id)
            faults.append((
                "test_bridge_bypass_cap_mismatch",
                f"test bridge {bridge_id} cap {cap_id} is not reached by matching A and B bypasses",
                [branch["entry_port"], branch["leaf_port"]] if branch else [],
                {"bridge": bridge_id, "cap_id": cap_id},
            ))
        for cap_id in sorted(set(by_cap_a) & set(by_cap_b)):
            ba, bb = by_cap_a[cap_id], by_cap_b[cap_id]
            key_a = [(p["segment"], p["pair"]) for p in ba["cable_pairs"]]
            key_b = [(p["segment"], p["pair"]) for p in bb["cable_pairs"]]
            same_pairs = key_a == key_b
            if not same_pairs:
                faults.append((
                    "test_bridge_pair_mismatch",
                    f"test bridge {bridge_id} A/B bypasses to cap {cap_id} use different cable pairs",
                    [ba["entry_port"], bb["entry_port"]],
                    {"bridge": bridge_id, "cap_id": cap_id, "a_pairs": key_a, "b_pairs": key_b},
                ))
            cap_legs_ok = ba.get("cap_leg") == "A" and bb.get("cap_leg") == "B"
            if not cap_legs_ok:
                faults.append((
                    "test_bridge_bypass_reversed",
                    f"test bridge {bridge_id} bypass A/B parity is crossed at cap {cap_id}: "
                    f"A bridge reaches cap {ba.get('cap_leg')}, B bridge reaches cap {bb.get('cap_leg')}",
                    sorted([ba["entry_port"], ba["leaf_port"], bb["entry_port"], bb["leaf_port"]]),
                    {
                        "bridge": bridge_id,
                        "cap_id": cap_id,
                        "a_branch_leaf": ba["leaf_port"],
                        "a_cap_leg": ba.get("cap_leg"),
                        "b_branch_leaf": bb["leaf_port"],
                        "b_cap_leg": bb.get("cap_leg"),
                    },
                ))
            swap_parity_ok = (ba.get("swap_count", 0) % 2) == 0 and (bb.get("swap_count", 0) % 2) == 0
            if not swap_parity_ok:
                faults.append((
                    "test_bridge_bypass_reversed",
                    f"test bridge {bridge_id} bypass contains an odd A/B swap before cap {cap_id}",
                    sorted([ba["entry_port"], ba["leaf_port"], bb["entry_port"], bb["leaf_port"]]),
                    {
                        "bridge": bridge_id,
                        "cap_id": cap_id,
                        "a_swaps": ba.get("swap_count"),
                        "b_swaps": bb.get("swap_count"),
                    },
                ))
            sealed = same_pairs and cap_legs_ok and swap_parity_ok
            reports.append(
                {
                    "bridge_id": bridge_id,
                    "joint": bva.joint,
                    "cap_id": cap_id,
                    "sealed": sealed,
                    "a": ba,
                    "b": bb,
                }
            )
    return reports, faults


def _all_branch_traces(
    graph: Graph, bridge_vertex: BridgeVertex, entry: PhysicalPort, main_exit: PhysicalPort
) -> list[dict[str, Any]]:
    result = []
    for edge in graph.vertices[bridge_vertex]:
        port = _other(edge, bridge_vertex)
        if not isinstance(port, PhysicalPort) or port in (entry, main_exit):
            continue
        trace = _trace_branch(graph, bridge_vertex, port, main_exit)
        if trace is not None:
            result.append(trace)
    return sorted(result, key=lambda item: (item["cap_id"] or "~", item["entry_port"]))
