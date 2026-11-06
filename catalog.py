"""
catalog.py  —  CAPA 2 de 3: el CATALOGO (A-Box)
------------------------------------------------
Los HECHOS de tu empresa hoy: que dominios existen, que servicios hay, quien es
dueño de que, que sub-agentes corren. NO es modelo (eso es `ontology.py`) ni
rastro de ejecucion (eso lo escribe `runtime.py`).

Ciclo de vida:  derivado por sincronizacion · cambia por horas/dias · nunca a mano.

Este es el cambio de fondo respecto a la version anterior: antes los 5 dominios,
4 servicios y 6 sub-agentes estaban HARDCODEADOS en el seed. Eso convierte al
grafo en una segunda fuente de verdad que se desincroniza de la real en dos
semanas — y a partir de ahi el orquestador rutea a dueños equivocados con toda
la confianza del mundo.

Aqui se leen de donde ya vive la verdad, en `catalog_source/`:

    domains.yaml            grupos del org chart / Backstage Groups
    CODEOWNERS              la propiedad (ya existe en tu monorepo)
    services/*.yaml         catalog-info.yaml, junto al codigo de cada servicio
    agents.yaml             manifiesto de despliegue de la plataforma

En produccion cambias estos lectores por llamadas a la API de Backstage, al
registro de servicios o al tagging del cloud. La forma del sync no cambia.
"""
from __future__ import annotations

import os
import re
from typing import Any

import yaml

SOURCE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog_source")


# ── lectores de las fuentes de verdad ─────────────────────────────────────
def read_domains(source_dir: str = SOURCE_DIR) -> list[dict]:
    with open(os.path.join(source_dir, "domains.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)["domains"]


def read_services(source_dir: str = SOURCE_DIR) -> list[dict]:
    svc_dir = os.path.join(source_dir, "services")
    out = []
    for fname in sorted(os.listdir(svc_dir)):
        if not fname.endswith((".yaml", ".yml")):
            continue
        with open(os.path.join(svc_dir, fname), encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        meta, spec = doc.get("metadata", {}), doc.get("spec", {})
        out.append({
            "id": meta.get("annotations", {}).get("idp/entity-id"),
            "name": meta.get("name"),
            "description": meta.get("description", ""),
            "stage": spec.get("lifecycle"),
            "path": spec.get("path"),
        })
    return out


def read_codeowners(source_dir: str = SOURCE_DIR) -> dict[str, str]:
    """path -> equipo de GitHub. Deriva la propiedad sin duplicarla."""
    owners = {}
    with open(os.path.join(source_dir, "CODEOWNERS"), encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = re.split(r"\s+", line)
            if len(parts) >= 2:
                owners[parts[0]] = parts[1]
    return owners


def read_agents(source_dir: str = SOURCE_DIR) -> dict[str, Any]:
    with open(os.path.join(source_dir, "agents.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── el sync ───────────────────────────────────────────────────────────────
class CatalogDriftError(Exception):
    """Un servicio sin dueño derivable. En real esto abre un ticket, no se ignora."""


def sync(g, source_dir: str = SOURCE_DIR, strict: bool = True) -> dict:
    """Sincroniza el catalogo al grafo. Devuelve un resumen de lo aplicado."""
    domains = read_domains(source_dir)
    services = read_services(source_dir)
    codeowners = read_codeowners(source_dir)
    agents = read_agents(source_dir)

    team_to_domain = {d["github_team"]: d["id"] for d in domains}
    summary = {"domains": 0, "services": 0, "subagents": 0, "edges": 0, "drift": []}

    # 1. Dominios
    for d in domains:
        g.add_node(d["id"], "Domain", content=d["description"],
                   name=d["name"], github_team=d["github_team"], layer="catalog")
        summary["domains"] += 1

    # 2. Servicios + propiedad DERIVADA de CODEOWNERS
    for s in services:
        g.add_node(s["id"], "Service", content=s["description"],
                   name=s["name"], path=s["path"], layer="catalog")
        summary["services"] += 1

        team = codeowners.get(s["path"])
        domain_id = team_to_domain.get(team) if team else None
        if not domain_id:
            summary["drift"].append(
                f"servicio '{s['name']}' ({s['path']}): sin dueño en CODEOWNERS")
            continue
        # UNA sola arista por hecho. La direccion inversa la resuelve GraphQuery.
        g.add_edge(domain_id, s["id"], edge_type="OWNS"); summary["edges"] += 1
        if s.get("stage"):
            g.add_edge(s["id"], s["stage"], edge_type="AT_STAGE"); summary["edges"] += 1

    # 3. Orquestador y sub-agentes
    orch = agents["orchestrator"]
    g.add_node(orch["id"], "Agent", content="Agente orquestador de la plataforma interna",
               name=orch["name"], framework=orch["framework"], layer="catalog")
    for sa in agents["subagents"]:
        g.add_node(sa["id"], "SubAgent", content=f"Sub-agente {sa['name']}",
                   name=sa["name"], layer="catalog")
        g.add_edge(orch["id"], sa["id"], edge_type="ORCHESTRATES"); summary["edges"] += 1
        for cap in sa["handles"]:
            g.add_edge(sa["id"], cap, edge_type="HANDLES"); summary["edges"] += 1
        summary["subagents"] += 1

    # 4. Las señales alimentan al orquestador
    for sig in ("SIG-REQUEST", "SIG-OWNER", "SIG-CI", "SIG-SEC",
                "SIG-ENV", "SIG-FREEZE", "SIG-APPROVAL"):
        g.add_edge(sig, orch["id"], edge_type="FEEDS"); summary["edges"] += 1

    if summary["drift"] and strict:
        raise CatalogDriftError(
            "El catalogo no cuadra con las fuentes de verdad:\n  - "
            + "\n  - ".join(summary["drift"])
        )
    return summary
