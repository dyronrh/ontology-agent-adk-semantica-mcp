"""
run_explorer.py
---------------
Semantica Knowledge Explorer + vigilante del fichero del grafo.

    python run_explorer.py --graph semantica_graph.json --port 8010

Por que existe. `semantica-explorer` lee el grafo UNA vez al arrancar y despues
trabaja en memoria. Las decisiones las escribe otro proceso (el shim MCP del
agente ADK) directamente en el fichero, asi que el Explorer nunca las ve hasta
que se reinicia.

Este lanzador monta la MISMA app del paquete (no lo parchea) y le añade una
tarea en segundo plano que vigila el fichero:

  - Si el fichero solo ha CRECIDO (lo normal: el agente registra decisiones),
    añade los nodos y aristas nuevos con `add_nodes` / `add_edges`. Esas
    llamadas disparan el `mutation_callback` que el Explorer ya tiene
    instalado, de modo que el indice de busqueda se actualiza y se emite
    `graph_mutation` por el WebSocket, como si la mutacion fuera local.
  - Si han DESAPARECIDO o CAMBIADO nodos/aristas (p. ej. un re-seed), recarga
    el grafo entero y emite `RELOAD_GRAPH`.

El shim escribe el fichero con open("w"), no de forma atomica: una lectura a
mitad de escritura da JSON invalido. En ese caso se ignora y se reintenta en el
siguiente ciclo; ademas se espera a que mtime/tamaño sean estables.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

log = logging.getLogger("explorer.watch")

POLL_SECONDS = 1.0


def _stamp(path: str):
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _read_graph(path: str):
    """Carga el fichero en un ContextGraph aparte. None si esta a medio escribir."""
    from semantica.context import ContextGraph

    try:
        with open(path, "r", encoding="utf-8") as f:
            json.load(f)  # validar antes: load_from_file no distingue truncado
    except (json.JSONDecodeError, UnicodeDecodeError, FileNotFoundError):
        return None
    fresh = ContextGraph()
    fresh.load_from_file(path)
    return fresh


def _sync(session, path: str) -> str:
    """Aplica al grafo del Explorer lo que ha cambiado en el fichero."""
    fresh = _read_graph(path)
    if fresh is None:
        return "partial"

    live = session.graph
    with live._lock:
        live_nodes = {nid: n.to_dict() for nid, n in live.nodes.items()}
        live_edges = {e.edge_id for e in live.edges}
    file_nodes = {nid: n.to_dict() for nid, n in fresh.nodes.items()}
    file_edges = {e.edge_id: e for e in fresh.edges}

    removed_nodes = live_nodes.keys() - file_nodes.keys()
    removed_edges = live_edges - file_edges.keys()
    changed_nodes = [nid for nid in live_nodes.keys() & file_nodes.keys()
                     if live_nodes[nid] != file_nodes[nid]]

    if removed_nodes or removed_edges or changed_nodes:
        # Re-seed u otra reescritura: no hay forma incremental fiable.
        live._suspend_mutation_callback = True
        try:
            live.load_from_file(path)
        finally:
            live._suspend_mutation_callback = False
        if callable(live.mutation_callback):
            live.mutation_callback("RELOAD_GRAPH", "", {
                "removed_nodes": len(removed_nodes),
                "removed_edges": len(removed_edges),
                "changed_nodes": len(changed_nodes),
            })
        return (f"recargado (-{len(removed_nodes)} nodos, -{len(removed_edges)} "
                f"aristas, ~{len(changed_nodes)} nodos cambiados)")

    new_nodes = [file_nodes[nid] for nid in file_nodes.keys() - live_nodes.keys()]
    new_edges = [file_edges[eid].to_dict() for eid in file_edges.keys() - live_edges]
    if not new_nodes and not new_edges:
        return "sin cambios"
    # nodos primero: si no, add_edges crea placeholders de tipo "entity"
    live.add_nodes(new_nodes)
    live.add_edges(new_edges)
    n_dec = sum(1 for n in new_nodes if n.get("type") == "decision")
    return f"+{len(new_nodes)} nodos ({n_dec} decisiones), +{len(new_edges)} aristas"


async def watch_graph_file(session, path: str) -> None:
    last_applied = _stamp(path)
    pending = None
    while True:
        await asyncio.sleep(POLL_SECONDS)
        stamp = _stamp(path)
        if stamp is None or stamp == last_applied:
            pending = None
            continue
        if stamp != pending:          # aun cambiando: esperar a que se asiente
            pending = stamp
            continue
        try:
            result = await asyncio.to_thread(_sync, session, path)
        except Exception:
            log.exception("Fallo sincronizando %s; se reintenta", path)
            continue
        if result == "partial":
            pending = None            # JSON a medio escribir: reintentar
            continue
        last_applied = stamp
        pending = None
        if result != "sin cambios":
            log.warning("Grafo sincronizado desde %s: %s", path, result)


def build_app(graph_path: str):
    from semantica.explorer.app import create_app
    from semantica.explorer.session import GraphSession

    session = GraphSession.from_file(graph_path)
    app = create_app(session=session)

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app_):
        async with original_lifespan(app_) as state:
            task = asyncio.create_task(watch_graph_file(session, graph_path))
            log.warning("Vigilando %s cada %.1fs", graph_path, POLL_SECONDS)
            try:
                yield state
            finally:
                task.cancel()

    app.router.lifespan_context = lifespan
    return app, session


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--graph", "-g", default="semantica_graph.json")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", "-p", type=int, default=8010)
    args = ap.parse_args()

    if not os.path.isfile(args.graph):
        raise SystemExit(f"No existe {args.graph}. Ejecuta primero: python seed_semantica.py")

    # Sin SEMANTICA_API_KEY el Explorer devuelve 503 en todo /api/* y la UI
    # (que no manda X-API-Key) se queda vacia. Igual que run_explorer.sh: en
    # loopback y sin clave, se opta por acceso anonimo. Fuera de loopback no.
    loopback = args.host in {"127.0.0.1", "::1", "localhost"}
    if loopback and not os.environ.get("SEMANTICA_API_KEY"):
        os.environ.setdefault("SEMANTICA_ALLOW_ANONYMOUS", "true")

    # El WebSocket /ws/graph-updates rechaza (4403 -> "403" en el log) todo
    # Origin que no este en la allowlist, y la de serie solo trae el dev server
    # de Vite (:5173). La UI que sirve este proceso llega con Origin
    # http://<host>:<port>, asi que sin esto no hay actualizaciones en vivo.
    if "ALLOWED_ORIGINS" not in os.environ and "EXPLORER_CORS_ORIGINS" not in os.environ:
        hosts = {args.host, "127.0.0.1", "localhost"} if loopback else {args.host}
        origins = [f"http://{h}:{args.port}" for h in sorted(hosts)]
        origins += ["http://localhost:5173", "http://127.0.0.1:5173"]
        os.environ["ALLOWED_ORIGINS"] = ",".join(origins)

    import uvicorn

    graph_path = os.path.abspath(args.graph)
    app, session = build_app(graph_path)
    stats = session.get_stats()
    print(f"Grafo cargado: {stats.get('node_count', 0)} nodos, "
          f"{stats.get('edge_count', 0)} aristas  ->  http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
