"""LOCALMEM graph store — NetworkX for behavioral pattern reasoning."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import networkx as nx

from .config import LocalmemConfig
from .models import GraphQuery

logger = logging.getLogger(__name__)


class GraphStore:
    """NetworkX-backed behavioral pattern graph with debounced persistence."""

    def __init__(self, config: LocalmemConfig):
        self.config = config
        self._graph = nx.DiGraph()
        self._lock = asyncio.Lock()
        self._dirty = False
        self._last_persist = 0.0
        self._persist_task: asyncio.Task | None = None

    async def initialize(self) -> None:
        path = Path(self.config.storage.graph_path)
        if path.exists():
            with open(path) as f:
                data = json.load(f)
            self._graph = nx.node_link_graph(data, directed=True)
            logger.info(
                f"Loaded graph: {self._graph.number_of_nodes()} nodes, "
                f"{self._graph.number_of_edges()} edges"
            )
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            logger.info("Initialized empty graph")

    async def shutdown(self) -> None:
        if self._persist_task and not self._persist_task.done():
            self._persist_task.cancel()
        if self._dirty:
            await self._persist_now()

    async def _schedule_persist(self) -> None:
        debounce = self.config.graph.persistence_debounce_seconds
        now = time.time()
        if now - self._last_persist < debounce:
            if self._persist_task is None or self._persist_task.done():
                self._persist_task = asyncio.create_task(self._debounced_persist())
        else:
            await self._persist_now()

    async def _debounced_persist(self) -> None:
        await asyncio.sleep(self.config.graph.persistence_debounce_seconds)
        if self._dirty:
            await self._persist_now()

    async def _persist_now(self) -> None:
        path = Path(self.config.storage.graph_path)
        data = nx.node_link_data(self._graph)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        self._dirty = False
        self._last_persist = time.time()
        logger.debug("Graph persisted to disk")

    async def add_node(
        self, node_id: str, attributes: dict[str, Any] | None = None
    ) -> None:
        async with self._lock:
            self._graph.add_node(node_id, **(attributes or {}))
            self._dirty = True
        await self._schedule_persist()

    async def add_edge(
        self,
        source: str,
        target: str,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        async with self._lock:
            self._graph.add_edge(source, target, **(attributes or {}))
            self._dirty = True
        await self._schedule_persist()

    async def remove_node(self, node_id: str) -> bool:
        async with self._lock:
            if node_id in self._graph:
                self._graph.remove_node(node_id)
                self._dirty = True
                await self._schedule_persist()
                return True
        return False

    async def query(self, q: GraphQuery) -> dict[str, Any]:
        """Execute a graph query. All reads are lock-free on a snapshot."""
        g = self._graph  # Read from live graph (NetworkX reads are safe if no concurrent mutation)

        if q.operation == "neighbors":
            if q.source_node not in g:
                return {"error": f"Node '{q.source_node}' not found"}
            neighbors = list(nx.ego_graph(g, q.source_node, radius=q.depth).nodes())
            return {
                "source": q.source_node,
                "neighbors": neighbors,
                "depth": q.depth,
            }

        elif q.operation == "path":
            if q.source_node not in g or q.target_node not in g:
                return {"error": "Source or target node not found"}
            try:
                path = nx.shortest_path(g, q.source_node, q.target_node)
                edges = []
                for i in range(len(path) - 1):
                    edge_data = g.get_edge_data(path[i], path[i + 1]) or {}
                    edges.append({
                        "from": path[i],
                        "to": path[i + 1],
                        **edge_data,
                    })
                return {"path": path, "edges": edges, "length": len(path) - 1}
            except nx.NetworkXNoPath:
                return {"path": None, "error": "No path exists"}

        elif q.operation == "community":
            if g.number_of_nodes() == 0:
                return {"communities": []}
            undirected = g.to_undirected()
            try:
                from networkx.algorithms.community import louvain_communities
                communities = louvain_communities(undirected)
                return {
                    "communities": [list(c) for c in communities],
                    "count": len(communities),
                }
            except Exception as e:
                return {"error": str(e)}

        elif q.operation == "centrality":
            if g.number_of_nodes() == 0:
                return {"centrality": {}}
            centrality = nx.degree_centrality(g)
            sorted_nodes = sorted(centrality.items(), key=lambda x: x[1], reverse=True)
            return {"centrality": dict(sorted_nodes[:20])}

        elif q.operation == "temporal":
            if not q.start_time or not q.end_time:
                return {"error": "temporal query requires start_time and end_time"}
            subgraph_nodes = [
                n
                for n, data in g.nodes(data=True)
                if data.get("timestamp", "") >= q.start_time
                and data.get("timestamp", "") <= q.end_time
            ]
            sub = g.subgraph(subgraph_nodes)
            return {
                "nodes": len(sub.nodes()),
                "edges": len(sub.edges()),
                "node_ids": list(sub.nodes()),
            }

        return {"error": f"Unknown operation: {q.operation}"}

    async def stats(self) -> dict[str, Any]:
        g = self._graph
        components = (
            nx.number_weakly_connected_components(g) if g.number_of_nodes() > 0 else 0
        )
        return {
            "nodes": g.number_of_nodes(),
            "edges": g.number_of_edges(),
            "density": nx.density(g) if g.number_of_nodes() > 1 else 0.0,
            "weakly_connected_components": components,
        }

    async def subgraph(
        self,
        center: str | None = None,
        depth: int = 2,
        limit: int = 200,
    ) -> dict[str, list[dict[str, Any]]]:
        g = self._graph
        if center and center in g:
            try:
                in_range = set(
                    nx.single_source_shortest_path_length(g, center, cutoff=depth).keys()
                )
            except (nx.NetworkXError, nx.NodeNotFound):
                in_range = {center}
        else:
            in_range = set(list(g.nodes)[:limit])

        sub = g.subgraph(in_range)
        nodes = [
            {"id": str(n), "attributes": dict(d)}
            for n, d in list(sub.nodes(data=True))[:limit]
        ]
        edges = [
            {"source": str(u), "target": str(v), "attributes": dict(d)}
            for u, v, d in list(sub.edges(data=True))[:limit]
        ]
        return {"nodes": nodes, "edges": edges}

    async def get_patterns(self, min_frequency: int = 2) -> list[dict[str, Any]]:
        """List pattern nodes by frequency/centrality."""
        g = self._graph
        patterns = []
        for node, data in g.nodes(data=True):
            if data.get("type") == "pattern":
                freq = data.get("frequency", 0)
                if freq >= min_frequency:
                    patterns.append({
                        "node_id": node,
                        "name": data.get("name", "unnamed"),
                        "frequency": freq,
                        "first_seen": data.get("first_seen", ""),
                        "connections": g.degree(node),
                    })
        patterns.sort(key=lambda p: p["frequency"], reverse=True)
        return patterns
