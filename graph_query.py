"""
graph_query.py
--------------
Capa de CONSULTA sobre el grafo. Existe por dos razones:

1. `ContextGraph.get_neighbors()` solo recorre aristas SALIENTES. Verificado:

       g.add_edge('D', 'S', edge_type='OWNS')
       g.get_neighbors('S')  ->  []          # no ve la arista entrante
       g.get_neighbors('D')  ->  [('S', 'OWNS')]

   La version anterior del repo compensaba eso duplicando cada arista a mano
   (`OWNS`/`IN_DOMAIN`, `HANDLES`/`HANDLED_BY`). Eso son dos fuentes de verdad
   para el mismo hecho: en cuanto alguien añade una sin la otra, el grafo miente.
   Aqui se declara la arista UNA vez y el indice resuelve las dos direcciones.

2. Las preguntas que el orquestador necesita hacer ("quien es el dueño",
   "que gates aplican") deben ser CONSULTAS AL GRAFO, no diccionarios de Python.

El indice se construye una vez sobre `graph.to_dict()`; llama a `refresh()` si
añades nodos o aristas despues de instanciarlo.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Optional


class GraphQuery:
    """Indice bidireccional + consultas de dominio sobre el grafo del IDP."""

    def __init__(self, graph):
        self._graph = graph
        self.refresh()

    # -- indice ------------------------------------------------------------
    def refresh(self) -> "GraphQuery":
        data = self._graph.to_dict()
        self._nodes = {n["id"]: n for n in data.get("nodes", [])}
        self._out = defaultdict(list)   # source -> [(edge_type, target, props)]
        self._in = defaultdict(list)    # target -> [(edge_type, source, props)]
        for e in data.get("edges", []):
            src, tgt = e.get("source"), e.get("target")
            etype = e.get("type")
            props = e.get("properties", {}) or {}
            self._out[src].append((etype, tgt, props))
            self._in[tgt].append((etype, src, props))
        return self

    # -- primitivas --------------------------------------------------------
    def node(self, node_id: str) -> Optional[dict]:
        return self._nodes.get(node_id)

    def props(self, node_id: str) -> dict:
        node = self._nodes.get(node_id)
        return (node or {}).get("properties", {}) or {}

    def name(self, node_id: str) -> str:
        return self.props(node_id).get("name") or node_id

    def nodes_of_type(self, node_type: str) -> list[dict]:
        return [n for n in self._nodes.values() if n.get("type") == node_type]

    def out(self, node_id: str, edge_type: Optional[str] = None,
            target_type: Optional[str] = None) -> list[str]:
        """IDs alcanzables por una arista SALIENTE."""
        return self._walk(self._out[node_id], edge_type, target_type)

    def inc(self, node_id: str, edge_type: Optional[str] = None,
            source_type: Optional[str] = None) -> list[str]:
        """IDs que apuntan a este nodo por una arista ENTRANTE."""
        return self._walk(self._in[node_id], edge_type, source_type)

    def _walk(self, bucket: Iterable[tuple], edge_type, node_type) -> list[str]:
        out = []
        for etype, other, _props in bucket:
            if edge_type and etype != edge_type:
                continue
            if node_type and (self._nodes.get(other, {}).get("type") != node_type):
                continue
            out.append(other)
        return out

    # -- consultas de dominio ---------------------------------------------
    def owner_domain(self, service_id: str) -> Optional[str]:
        """Dominio dueño de un servicio, via la arista ENTRANTE Domain-[OWNS]->Service."""
        owners = self.inc(service_id, edge_type="OWNS", source_type="Domain")
        return owners[0] if owners else None

    def capability_for_intent(self, intent: str) -> Optional[str]:
        """Capacidad del ciclo de vida que realiza una intencion.

        Reemplaza al diccionario INTENT_TO_CAPABILITY que vivia en Python:
        ahora son aristas Intent-[REALIZES]->Capability y se pueden añadir
        intenciones sin tocar codigo.
        """
        node_id = intent if intent.startswith("INT-") else f"INT-{intent.upper()}"
        caps = self.out(node_id, edge_type="REALIZES", target_type="Capability")
        return caps[0] if caps else None

    def handler_subagent(self, capability_id: str) -> Optional[str]:
        """Sub-agente que maneja una capacidad, via la arista ENTRANTE HANDLES."""
        handlers = self.inc(capability_id, edge_type="HANDLES", source_type="SubAgent")
        return handlers[0] if handlers else None

    def gates_for(self, capability_id: str, env: str) -> list[dict]:
        """Politicas aplicables a (capacidad, entorno), EN ORDEN.

        Este es el metodo que justifica todo el rediseño. Antes, el orden y la
        lista de gates estaban escritos en el prompt del agente
        ("ownership -> CI -> seguridad -> freeze/aprobacion"). Ahora salen del
        grafo: Policy-[APPLIES_TO]->Capability, filtradas por `env_scope` y
        ordenadas por `gate_order`. Añadir una politica es añadir un nodo.
        """
        gates = []
        for pol_id in self.inc(capability_id, edge_type="APPLIES_TO", source_type="Policy"):
            p = self.props(pol_id)
            scope = p.get("env_scope") or []
            if scope and env not in scope:
                continue          # p. ej. POL-FREEZE solo aplica en prod
            gates.append({
                "id": pol_id,
                "name": p.get("name", pol_id),
                "order": int(p.get("gate_order", 999)),
                "expression": p.get("expression", ""),
                "env_scope": scope,
                "reads": self.out(pol_id, edge_type="EVALUATES", target_type="Signal"),
                "description": (self.node(pol_id) or {}).get("content", ""),
            })
        return sorted(gates, key=lambda g: g["order"])

    def lifecycle_order(self) -> list[str]:
        """Capacidades ordenadas por la cadena NEXT_STAGE (Plan..Retire)."""
        caps = {n["id"] for n in self.nodes_of_type("Capability")}
        starts = [c for c in caps if not self.inc(c, edge_type="NEXT_STAGE")]
        if not starts:
            return sorted(caps)
        chain, cur = [], starts[0]
        while cur and cur not in chain:
            chain.append(cur)
            nxt = self.out(cur, edge_type="NEXT_STAGE", target_type="Capability")
            cur = nxt[0] if nxt else None
        return chain

    # -- explicabilidad ----------------------------------------------------
    def explain(self, node_id: str) -> dict:
        """Todo lo que el grafo sabe de un nodo, en ambas direcciones."""
        return {
            "id": node_id,
            "type": (self.node(node_id) or {}).get("type"),
            "name": self.name(node_id),
            "outgoing": [(t, o) for t, o, _ in self._out[node_id]],
            "incoming": [(t, s) for t, s, _ in self._in[node_id]],
        }
