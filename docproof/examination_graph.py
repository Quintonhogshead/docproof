"""A sparse graph over the document objects phase one can represent honestly."""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import DocumentModel
from .site_models import SiteAnchor


@dataclass(frozen=True)
class GraphNode:
    node_id: str
    node_type: str
    anchor: SiteAnchor | None = None
    properties: dict = field(default_factory=dict)


@dataclass(frozen=True)
class GraphEdge:
    source: str
    target: str
    edge_type: str
    properties: dict = field(default_factory=dict)


@dataclass
class ExaminationGraph:
    nodes: dict[str, GraphNode] = field(default_factory=dict)
    edges: list[GraphEdge] = field(default_factory=list)

    @classmethod
    def from_document(cls, doc: DocumentModel) -> "ExaminationGraph":
        graph = cls()
        root = "document"
        graph.nodes[root] = GraphNode(
            root, "document", properties={"source_path": doc.source_path})
        previous = None
        for para in doc.paragraphs:
            node_id = f"paragraph:{para.para_id}"
            graph.nodes[node_id] = GraphNode(
                node_id, "paragraph",
                SiteAnchor(part=para.part, paragraph_id=para.para_id,
                           start_offset=0, end_offset=len(para.text)),
                {"style": para.style, "location": para.location,
                 "reviewable": para.reviewable})
            graph.edges.append(GraphEdge(root, node_id, "contains"))
            if previous is not None:
                graph.edges.append(GraphEdge(previous, node_id, "next"))
            previous = node_id
        return graph

    def counts(self) -> dict:
        node_types: dict[str, int] = {}
        edge_types: dict[str, int] = {}
        for node in self.nodes.values():
            node_types[node.node_type] = node_types.get(node.node_type, 0) + 1
        for edge in self.edges:
            edge_types[edge.edge_type] = edge_types.get(edge.edge_type, 0) + 1
        return {"nodes": len(self.nodes), "edges": len(self.edges),
                "node_types": dict(sorted(node_types.items())),
                "edge_types": dict(sorted(edge_types.items()))}
