"""Strict YAML decoding and manifest schema validation."""

from __future__ import annotations

from typing import Any, Iterable

import yaml

from .models import (
    CableLeg,
    CapSpec,
    Endpoint,
    JointSpec,
    Leg,
    Manifest,
    ManifestError,
    OpenEndSpec,
    PairSpec,
    Polarity,
    RouteSpec,
    SegmentSpec,
    SpliceSpec,
    TestBridgeSpec,
    TerminalPair,
    ValidationIssue,
)


class StrictLoader(yaml.SafeLoader):
    """SafeLoader with duplicate mapping detection and no implicit global tags."""


def _construct_mapping(loader: yaml.Loader, node: yaml.MappingNode, deep: bool = False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            hash(key)
        except TypeError as exc:  # pragma: no cover - defensive YAML rejection
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"unhashable mapping key: {exc}",
                key_node.start_mark,
            ) from exc
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate mapping key {key!r}",
                key_node.start_mark,
            )
        value = loader.construct_object(value_node, deep=deep)
        mapping[key] = value
    return mapping


StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def parse_yaml(text: str) -> Manifest:
    """Parse *text* and validate the complete manifest schema."""

    validator = _ManifestValidator()
    try:
        documents = list(yaml.load_all(text, Loader=StrictLoader))  # noqa: S506 - StrictLoader is SafeLoader-derived
    except RecursionError as exc:
        raise ManifestError([
            ValidationIssue("invalid_yaml", f"YAML contains cyclic aliases or excessive nesting: {exc}", "")
        ]) from exc
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        loc = f"line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        issue = ValidationIssue("invalid_yaml", f"YAML could not be parsed: {exc}", loc)
        raise ManifestError([issue]) from exc

    if len(documents) > 1:
        raise ManifestError([
            ValidationIssue(
                "invalid_yaml",
                "a splicing manifest must contain exactly one YAML document",
                "",
            )
        ])
    data = documents[0] if documents else None
    if not isinstance(data, dict):
        validator.fail("manifest must be a mapping", "$")
        raise ManifestError(validator.issues)
    return validator.validate(data)


class _ManifestValidator:
    REQUIRED_TOP_LEVEL = {"version", "routes", "segments", "joints", "caps"}
    ALLOWED_TOP_LEVEL = REQUIRED_TOP_LEVEL | {"open_ends"}

    def __init__(self) -> None:
        self.issues: list[ValidationIssue] = []

    def fail(self, message: str, location: str = "", code: str = "invalid_manifest") -> None:
        self.issues.append(ValidationIssue(code, message, location))

    def validate(self, data: dict[str, Any]) -> Manifest:
        unknown = set(data) - self.ALLOWED_TOP_LEVEL
        for key in sorted(unknown):
            self.fail(f"unknown top-level field {key!r}", f"$.{key}", "unknown_field")
        for key in sorted(self.REQUIRED_TOP_LEVEL - set(data)):
            self.fail(f"required field {key!r} is missing", f"$.{key}", "missing_field")

        # bool is a subclass of int in Python (True == 1), so check the exact type:
        # only the integer literal 1 is the manifest version. ``true`` and ``1.0`` are rejected.
        version = data.get("version")
        if type(version) is not int or version != 1:
            self.fail(
                "version must be the integer literal 1 (booleans and floats such as true or 1.0 are invalid)",
                "$.version",
                "invalid_version",
            )

        routes = self._routes(data.get("routes", []))
        if data.get("routes") == []:
            self.fail(
                "a splicing manifest must declare at least one office-to-subscriber route; "
                "an empty book cannot be proved as serviceable",
                "$.routes",
                "empty_routes",
            )
        segments = self._segments(data.get("segments", []))
        joints = self._joints(data.get("joints", []))
        caps = self._caps(data.get("caps", []))
        open_ends = self._open_ends(data.get("open_ends", []))

        if self.issues:
            raise ManifestError(self.issues)

        self._global_cross_checks(routes, segments, joints, caps, open_ends)
        if self.issues:
            raise ManifestError(self.issues)

        return Manifest(
            version=1,
            routes=routes,
            segments=segments,
            joints=joints,
            caps=caps,
            open_ends=open_ends,
        )

    def require_mapping(self, value: Any, location: str) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            self.fail("expected a mapping", location, "invalid_type")
            return None
        return value

    def require_list(self, value: Any, location: str) -> list[Any] | None:
        if not isinstance(value, list):
            self.fail("expected a list", location, "invalid_type")
            return None
        return value

    def require_string(self, value: Any, location: str, field: str) -> str | None:
        if not isinstance(value, str) or not value:
            self.fail(f"{field} must be a non-empty string", location, "invalid_type")
            return None
        return value

    def reject_unknown(self, data: dict[str, Any], allowed: set[str], location: str) -> None:
        for key in sorted(set(data) - allowed):
            self.fail(f"unknown field {key!r}", f"{location}.{key}", "unknown_field")

    def require_fields(self, data: dict[str, Any], fields: Iterable[str], location: str) -> None:
        for field in fields:
            if field not in data:
                self.fail(f"required field {field!r} is missing", f"{location}.{field}", "missing_field")

    def _endpoint(self, value: Any, location: str) -> Endpoint | None:
        data = self.require_mapping(value, location)
        if data is None:
            return None
        self.reject_unknown(data, {"node", "port"}, location)
        self.require_fields(data, ("node", "port"), location)
        node = self.require_string(data.get("node"), f"{location}.node", "node")
        port = self.require_string(data.get("port"), f"{location}.port", "port")
        if node is None or port is None:
            return None
        return Endpoint(node=node, port=port)

    def _terminal_pair(self, value: Any, location: str) -> TerminalPair | None:
        data = self.require_mapping(value, location)
        if data is None:
            return None
        self.reject_unknown(data, {"a", "b"}, location)
        self.require_fields(data, ("a", "b"), location)
        a = self._endpoint(data.get("a"), f"{location}.a")
        b = self._endpoint(data.get("b"), f"{location}.b")
        if a is None or b is None:
            return None
        if a == b:
            self.fail("terminal A and B must be distinct physical ports", location, "terminal_pair_error")
            return None
        return TerminalPair(a=a, b=b)

    def _cable_leg(self, value: Any, location: str) -> CableLeg | None:
        data = self.require_mapping(value, location)
        if data is None:
            return None
        self.reject_unknown(data, {"endpoints"}, location)
        self.require_fields(data, ("endpoints",), location)
        endpoints_raw = self.require_list(data.get("endpoints"), f"{location}.endpoints")
        if endpoints_raw is None:
            return None
        if len(endpoints_raw) != 2:
            self.fail("a cable conductor must list exactly two endpoints", f"{location}.endpoints", "conductor_endpoint_error")
            return None
        endpoints = [
            self._endpoint(endpoint_raw, f"{location}.endpoints[{index}]")
            for index, endpoint_raw in enumerate(endpoints_raw)
        ]
        if any(endpoint is None for endpoint in endpoints):
            return None
        first, second = endpoints  # type: ignore[misc]
        if first.node == second.node:
            self.fail("a conductor must connect two different nodes", location, "conductor_endpoint_error")
            return None
        return CableLeg(endpoints=(first, second))  # type: ignore[arg-type]

    def _routes(self, raw: Any) -> tuple[RouteSpec, ...]:
        items = self.require_list(raw, "$.routes")
        if items is None:
            return ()
        result: list[RouteSpec] = []
        seen: set[str] = set()
        for index, item in enumerate(items):
            loc = f"$.routes[{index}]"
            data = self.require_mapping(item, loc)
            if data is None:
                continue
            self.reject_unknown(data, {"id", "expected_polarity", "office", "subscriber"}, loc)
            self.require_fields(data, ("id", "expected_polarity", "office", "subscriber"), loc)
            route_id = self.require_string(data.get("id"), f"{loc}.id", "id")
            polarity = data.get("expected_polarity")
            if polarity not in ("normal", "reversed"):
                self.fail(
                    "expected_polarity must be 'normal' or 'reversed'",
                    f"{loc}.expected_polarity",
                    "invalid_polarity",
                )
                polarity = None
            office = self._terminal_pair(data.get("office"), f"{loc}.office")
            subscriber = self._terminal_pair(data.get("subscriber"), f"{loc}.subscriber")
            if route_id:
                if route_id in seen:
                    self.fail(f"duplicate route id {route_id!r}", f"{loc}.id", "duplicate_id")
                seen.add(route_id)
            if all(value is not None for value in (route_id, polarity, office, subscriber)):
                result.append(
                    RouteSpec(
                        id=route_id,
                        expected_polarity=polarity,  # type: ignore[arg-type]
                        office=office,  # type: ignore[arg-type]
                        subscriber=subscriber,  # type: ignore[arg-type]
                    )
                )
        return tuple(result)

    def _segments(self, raw: Any) -> tuple[SegmentSpec, ...]:
        items = self.require_list(raw, "$.segments")
        if items is None:
            return ()
        result: list[SegmentSpec] = []
        seen: set[str] = set()
        for index, item in enumerate(items):
            loc = f"$.segments[{index}]"
            data = self.require_mapping(item, loc)
            if data is None:
                continue
            self.reject_unknown(data, {"id", "pairs"}, loc)
            self.require_fields(data, ("id", "pairs"), loc)
            segment_id = self.require_string(data.get("id"), f"{loc}.id", "id")
            pair_items = self.require_list(data.get("pairs"), f"{loc}.pairs")
            pairs: list[PairSpec] = []
            pair_seen: set[str] = set()
            if pair_items is not None:
                for p_index, p_raw in enumerate(pair_items):
                    p_loc = f"{loc}.pairs[{p_index}]"
                    p_data = self.require_mapping(p_raw, p_loc)
                    if p_data is None:
                        continue
                    self.reject_unknown(p_data, {"pair", "a", "b"}, p_loc)
                    self.require_fields(p_data, ("pair", "a", "b"), p_loc)
                    pair_id = self.require_string(p_data.get("pair"), f"{p_loc}.pair", "pair")
                    a = self._cable_leg(p_data.get("a"), f"{p_loc}.a")
                    b = self._cable_leg(p_data.get("b"), f"{p_loc}.b")
                    if pair_id:
                        if pair_id in pair_seen:
                            self.fail(
                                f"duplicate pair id {pair_id!r} in segment {segment_id!r}",
                                f"{p_loc}.pair",
                                "duplicate_pair",
                            )
                        pair_seen.add(pair_id)
                    if all(v is not None for v in (pair_id, a, b)):
                        pairs.append(PairSpec(pair=pair_id, a=a, b=b))  # type: ignore[arg-type]
                        ends = {endpoint.node for endpoint in a.endpoints}  # type: ignore[union-attr]
                        ends_b = {endpoint.node for endpoint in b.endpoints}  # type: ignore[union-attr]
                        if ends != ends_b:
                            self.fail(
                                "A and B conductors of one pair must connect the same two nodes",
                                p_loc,
                                "pair_endpoint_error",
                            )
            if segment_id:
                if segment_id in seen:
                    self.fail(f"duplicate segment id {segment_id!r}", f"{loc}.id", "duplicate_id")
                seen.add(segment_id)
            if segment_id is not None:
                result.append(SegmentSpec(id=segment_id, pairs=tuple(pairs)))
        return tuple(result)

    def _joints(self, raw: Any) -> tuple[JointSpec, ...]:
        items = self.require_list(raw, "$.joints")
        if items is None:
            return ()
        result: list[JointSpec] = []
        seen: set[str] = set()
        bridge_seen: set[str] = set()
        for index, item in enumerate(items):
            loc = f"$.joints[{index}]"
            data = self.require_mapping(item, loc)
            if data is None:
                continue
            self.reject_unknown(data, {"id", "splices", "test_bridges"}, loc)
            self.require_fields(data, ("id",), loc)
            joint_id = self.require_string(data.get("id"), f"{loc}.id", "id")
            splices = self._splices(data.get("splices", []), loc, joint_id or f"#{index}")
            bridges = self._test_bridges(
                data.get("test_bridges", []), loc, joint_id or f"#{index}", bridge_seen
            )
            if joint_id:
                if joint_id in seen:
                    self.fail(f"duplicate joint id {joint_id!r}", f"{loc}.id", "duplicate_id")
                seen.add(joint_id)
                result.append(JointSpec(id=joint_id, splices=splices, test_bridges=bridges))
        return tuple(result)

    def _splice_port(self, value: Any, location: str, joint_id: str) -> tuple[str, str] | None:
        data = self.require_mapping(value, location)
        if data is None:
            return None
        self.reject_unknown(data, {"node", "port"}, location)
        self.require_fields(data, ("node", "port"), location)
        node = self.require_string(data.get("node"), f"{location}.node", "node")
        port = self.require_string(data.get("port"), f"{location}.port", "port")
        if node is not None and node != joint_id:
            self.fail(
                f"splice is attached to {node!r}, not joint {joint_id!r}",
                f"{location}.node",
                "joint_reference_error",
            )
            node = None
        if node is None or port is None:
            return None
        return node, port

    def _splices(self, raw: Any, loc: str, joint_id: str) -> tuple[SpliceSpec, ...]:
        items = self.require_list(raw, f"{loc}.splices")
        if items is None:
            return ()
        result: list[SpliceSpec] = []
        seen: set[tuple[str, str]] = set()
        for index, item in enumerate(items):
            s_loc = f"{loc}.splices[{index}]"
            data = self.require_mapping(item, s_loc)
            if data is None:
                continue
            self.reject_unknown(data, {"a", "b"}, s_loc)
            self.require_fields(data, ("a", "b"), s_loc)
            a = self._splice_port(data.get("a"), f"{s_loc}.a", joint_id)
            b = self._splice_port(data.get("b"), f"{s_loc}.b", joint_id)
            if a is not None and a in seen:
                self.fail(f"joint port {a[1]!r} is used more than once", f"{s_loc}.a", "duplicate_port")
            if b is not None and b in seen:
                self.fail(f"joint port {b[1]!r} is used more than once", f"{s_loc}.b", "duplicate_port")
            if a is not None and b is not None:
                if a == b:
                    self.fail("a splice cannot connect a port to itself", s_loc, "self_loop")
                seen.add(a)
                seen.add(b)
                result.append(SpliceSpec(index=len(result), a=a, b=b))
        return tuple(result)

    def _test_bridges(
        self, raw: Any, loc: str, joint_id: str, seen_bridges: set[str]
    ) -> tuple[TestBridgeSpec, ...]:
        items = self.require_list(raw, f"{loc}.test_bridges")
        if items is None:
            return ()
        result: list[TestBridgeSpec] = []
        for index, item in enumerate(items):
            b_loc = f"{loc}.test_bridges[{index}]"
            data = self.require_mapping(item, b_loc)
            if data is None:
                continue
            self.reject_unknown(data, {"id", "a_ports", "b_ports"}, b_loc)
            self.require_fields(data, ("id", "a_ports", "b_ports"), b_loc)
            bridge_id = self.require_string(data.get("id"), f"{b_loc}.id", "id")
            a_ports = self._port_name_list(data.get("a_ports"), f"{b_loc}.a_ports")
            b_ports = self._port_name_list(data.get("b_ports"), f"{b_loc}.b_ports")
            if bridge_id:
                if bridge_id in seen_bridges:
                    self.fail(f"duplicate test bridge id {bridge_id!r}", f"{b_loc}.id", "duplicate_id")
                seen_bridges.add(bridge_id)
                all_ports = [*a_ports, *b_ports]
                for port in all_ports:
                    if all_ports.count(port) > 1:
                        self.fail(
                            f"bridge port {port!r} may not appear in both or repeated groups",
                            b_loc,
                            "duplicate_port",
                        )
                if len(a_ports) < 3 or len(b_ports) < 3:
                    self.fail(
                        "a named test bridge needs at least three A and three B physical ports",
                        b_loc,
                        "invalid_bridge_degree",
                    )
                result.append(
                    TestBridgeSpec(
                        id=bridge_id, joint=joint_id, a_ports=tuple(a_ports), b_ports=tuple(b_ports)
                    )
                )
        return tuple(result)

    def _port_name_list(self, raw: Any, location: str) -> list[str]:
        items = self.require_list(raw, location)
        if items is None:
            return []
        result: list[str] = []
        seen: set[str] = set()
        for index, item in enumerate(items):
            p_loc = f"{location}[{index}]"
            if not isinstance(item, str) or not item:
                self.fail("port must be a non-empty string", p_loc, "invalid_type")
                continue
            if item in seen:
                self.fail(f"duplicate port {item!r}", p_loc, "duplicate_port")
            seen.add(item)
            result.append(item)
        return result

    def _caps(self, raw: Any) -> tuple[CapSpec, ...]:
        items = self.require_list(raw, "$.caps")
        if items is None:
            return ()
        result: list[CapSpec] = []
        seen: set[str] = set()
        for index, item in enumerate(items):
            loc = f"$.caps[{index}]"
            data = self.require_mapping(item, loc)
            if data is None:
                continue
            self.reject_unknown(data, {"id", "a", "b"}, loc)
            self.require_fields(data, ("id", "a", "b"), loc)
            cap_id = self.require_string(data.get("id"), f"{loc}.id", "id")
            a = self._endpoint(data.get("a"), f"{loc}.a")
            b = self._endpoint(data.get("b"), f"{loc}.b")
            if cap_id:
                if cap_id in seen:
                    self.fail(f"duplicate cap id {cap_id!r}", f"{loc}.id", "duplicate_id")
                seen.add(cap_id)
            if all(v is not None for v in (cap_id, a, b)):
                if a.node != b.node:  # type: ignore[union-attr]
                    self.fail("the sealed A/B ends of a cap must be on one cap node", loc, "cap_endpoint_error")
                if (a.node, a.port) == (b.node, b.port):  # type: ignore[union-attr]
                    self.fail("cap A and B must use distinct physical ports", loc, "cap_endpoint_error")
                result.append(CapSpec(id=cap_id, a=a, b=b))  # type: ignore[arg-type]
        return tuple(result)

    def _open_ends(self, raw: Any) -> tuple[OpenEndSpec, ...]:
        if raw in (None, []):
            return ()
        items = self.require_list(raw, "$.open_ends")
        if items is None:
            return ()
        result: list[OpenEndSpec] = []
        seen: set[str] = set()
        for index, item in enumerate(items):
            loc = f"$.open_ends[{index}]"
            data = self.require_mapping(item, loc)
            if data is None:
                continue
            self.reject_unknown(data, {"id", "node", "ports"}, loc)
            self.require_fields(data, ("id", "node", "ports"), loc)
            end_id = self.require_string(data.get("id"), f"{loc}.id", "id")
            node = self.require_string(data.get("node"), f"{loc}.node", "node")
            ports = self._port_name_list(data.get("ports"), f"{loc}.ports")
            if not ports:
                self.fail("an open end must name at least one physical port", loc, "invalid_open_end")
            if end_id:
                if end_id in seen:
                    self.fail(f"duplicate open end id {end_id!r}", f"{loc}.id", "duplicate_id")
                seen.add(end_id)
            if end_id and node and ports:
                result.append(OpenEndSpec(id=end_id, node=node, ports=tuple(ports)))
        return tuple(result)

    def _global_cross_checks(
        self,
        routes: tuple[RouteSpec, ...],
        segments: tuple[SegmentSpec, ...],
        joints: tuple[JointSpec, ...],
        caps: tuple[CapSpec, ...],
        open_ends: tuple[OpenEndSpec, ...] = (),
    ) -> None:
        node_roles: dict[str, str] = {}
        all_ports: dict[tuple[str, str], str] = {}
        conductor_pairs: set[tuple[str, str, tuple[str, ...]]] = set()
        conductor_ends: set[tuple[str, str]] = set()
        local_legs: dict[tuple[str, str], Leg] = {}

        def role(node: str, role_name: str, location: str) -> None:
            previous = node_roles.get(node)
            if previous is not None and previous != role_name:
                self.fail(
                    f"node {node!r} is both {previous} and {role_name}",
                    location,
                    "node_role_conflict",
                )
            node_roles[node] = role_name

        def claim_port(node: str, port: str, owner: str, location: str) -> None:
            key = (node, port)
            previous = all_ports.get(key)
            if previous is not None and previous != owner:
                self.fail(
                    f"physical port {node}/{port} is claimed by {previous!r} and {owner!r}",
                    location,
                    "duplicate_terminal_port",
                )
            all_ports[key] = owner

        for route in routes:
            role(route.office.a.node, "office", f"route {route.id} office")
            role(route.subscriber.a.node, "subscriber", f"route {route.id} subscriber")
            if route.office.a.node == route.subscriber.a.node:
                self.fail(
                    f"route {route.id} office and subscriber must be different nodes",
                    f"route {route.id}",
                    "route_endpoint_error",
                )
            for ep, leg in ((route.office.a, "A"), (route.office.b, "B"),
                            (route.subscriber.a, "A"), (route.subscriber.b, "B")):
                claim_port(ep.node, ep.port, f"route:{route.id}:{leg}", f"route {route.id}")

        for cap in caps:
            role(cap.a.node, "cap", f"cap {cap.id}")
            claim_port(cap.a.node, cap.a.port, f"cap:{cap.id}:A", f"cap {cap.id}")
            claim_port(cap.b.node, cap.b.port, f"cap:{cap.id}:B", f"cap {cap.id}")

        cap_nodes = [cap.a.node for cap in caps]
        for node in sorted({node for node in cap_nodes if cap_nodes.count(node) > 1}):
            owners = sorted({cap.id for cap in caps if cap.a.node == node})
            self.fail(
                f"cap node {node!r} cannot host multiple caps: {', '.join(owners)}",
                f"cap {owners[0]}",
                "node_role_conflict",
            )

        open_ports: set[tuple[str, str]] = set()
        for open_end in open_ends:
            role(open_end.node, "open_end", f"open end {open_end.id}")
            for port in open_end.ports:
                key = (open_end.node, port)
                claim_port(open_end.node, port, f"open_end:{open_end.id}", f"open end {open_end.id}")
                open_ports.add(key)

        for joint in joints:
            role(joint.id, "joint", f"joint {joint.id}")

        # A physical joint port may be listed in one splice entry OR one named bridge group.
        # Repeating it across splice entries, bridges, or a/b groups is a structural defect of the
        # single joint table: reject the whole book as 422 instead of interpreting it as a loop.
        for joint in joints:
            port_owner: dict[str, str] = {}

            def claim_joint_port(port: str, owner: str, location: str) -> None:
                previous = port_owner.get(port)
                if previous is not None and previous != owner:
                    self.fail(
                        f"joint {joint.id} physical port {port!r} is written twice: "
                        f"once in {previous!r} and once in {owner!r}",
                        location,
                        "duplicate_port",
                    )
                else:
                    port_owner[port] = owner

            for splice in joint.splices:
                owner = f"splice[{splice.index}]"
                claim_joint_port(splice.a[1], owner, f"joint {joint.id}")
                claim_joint_port(splice.b[1], owner, f"joint {joint.id}")
            for bridge in joint.test_bridges:
                for group, ports in (("a_ports", bridge.a_ports), ("b_ports", bridge.b_ports)):
                    owner = f"test_bridge:{bridge.id}:{group}"
                    for port in ports:
                        claim_joint_port(port, owner, f"bridge {bridge.id}")

        for segment in segments:
            for pair in segment.pairs:
                for cable_leg in (pair.a, pair.b):
                    for ep in cable_leg.endpoints:
                        if ep.node not in node_roles:
                            self.fail(
                                f"cable endpoint node {ep.node!r} is not declared",
                                f"segment {segment.id}/{pair.pair}",
                                "undeclared_node",
                            )
                for cable_leg, leg in ((pair.a, "A"), (pair.b, "B")):
                    for ep in cable_leg.endpoints:
                        key = (ep.node, ep.port)
                        previous_leg = local_legs.get(key)
                        if previous_leg is not None and previous_leg != leg:
                            self.fail(
                                f"physical port {ep.node}/{ep.port} is labelled both {previous_leg} and {leg}",
                                f"segment {segment.id}/{pair.pair}.{leg}",
                                "port_leg_conflict",
                            )
                        local_legs[key] = leg
                        if key in conductor_ends:
                            self.fail(
                                f"physical port {ep.node}/{ep.port} terminates more than one conductor",
                                f"segment {segment.id}/{pair.pair}.{leg}",
                                "duplicate_conductor_port",
                            )
                        conductor_ends.add(key)
                conductor_key = (
                    segment.id,
                    pair.pair,
                    tuple(sorted(
                        f"{leg}:{ep.node}/{ep.port}"
                        for leg, cable_leg in (("A", pair.a), ("B", pair.b))
                        for ep in cable_leg.endpoints
                    )),
                )
                if conductor_key in conductor_pairs:
                    self.fail(
                        f"duplicate conductor {segment.id}/{pair.pair}",
                        f"segment {segment.id}/{pair.pair}",
                        "duplicate_conductor",
                    )
                conductor_pairs.add(conductor_key)

        for route in routes:
            for endpoint, expected in (
                (route.office.a, "A"),
                (route.office.b, "B"),
                (route.subscriber.a, "A"),
                (route.subscriber.b, "B"),
            ):
                actual = local_legs.get((endpoint.node, endpoint.port))
                if actual is not None and actual != expected:
                    self.fail(
                        f"route {route.id} terminal {endpoint.node}/{endpoint.port} "
                        f"is physical {actual}, not declared {expected}",
                        f"route {route.id}",
                        "port_leg_conflict",
                    )

        for cap in caps:
            for endpoint, expected in ((cap.a, "A"), (cap.b, "B")):
                actual = local_legs.get((endpoint.node, endpoint.port))
                if actual is not None and actual != expected:
                    self.fail(
                        f"cap {cap.id} terminal {endpoint.node}/{endpoint.port} "
                        f"is physical {actual}, not declared {expected}",
                        f"cap {cap.id}",
                        "port_leg_conflict",
                    )

        # Every conductor end at a joint has to be listed once in a splice or named bridge.
        for joint in joints:
            declared = set()
            for splice in joint.splices:
                declared.add(splice.a)
                declared.add(splice.b)
            for bridge in joint.test_bridges:
                for port in (*bridge.a_ports, *bridge.b_ports):
                    declared.add((joint.id, port))
            at_joint = {key for key in conductor_ends if key[0] == joint.id}
            for key in sorted(at_joint - declared):
                self.fail(
                    f"joint port {key[0]}/{key[1]} has a cable conductor but no splice or named test bridge",
                    f"joint {joint.id}",
                    "unconnected_joint_port",
                )
            for key in sorted(declared - at_joint):
                self.fail(
                    f"joint map references port {key[0]}/{key[1]} without a cable conductor",
                    f"joint {joint.id}",
                    "phantom_joint_port",
                )
