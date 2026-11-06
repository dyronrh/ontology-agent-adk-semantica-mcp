"""
semantica_mcp_shim.py
---------------------
Servidor MCP de Semantica con la persistencia arreglada, y con una herramienta
de gobierno que el agente ADK puede CONSULTAR.

    python semantica_mcp_shim.py        # habla MCP por stdio

Por que existe. El servidor que trae `semantica 0.6.6` no puede sostener el
flujo ADK -> grafo -> Explorer. Tres fallos, verificados:

  1. Lee la variable `SEMANTICA_KG_PATH`, no `SEMANTICA_GRAPH` (que era la que
     usaban `.mcp.json` y `adk_orchestrator.py`; esa cadena no aparece en todo
     el paquete).
  2. Aun con el nombre correcto llama a `ContextGraph.load()`, metodo que NO
     existe — la API real es `load_from_file`. El `except Exception` se lo traga
     y el servidor arranca SIEMPRE con el grafo vacio.
  3. `record_decision` solo muta memoria. Nada se escribe al disco, y cuando ADK
     cierra el proceso stdio las decisiones se pierden. `export_graph` devuelve
     el grafo como texto en la respuesta, no acepta ruta.

Este shim no parchea el paquete instalado: reusa su bucle JSON-RPC y su tabla de
herramientas, y corrige lo minimo.
"""
from __future__ import annotations

import os
import sys
import threading

from semantica import mcp_server as mcp

import signals
from graph_query import GraphQuery

# OJO: `from semantica.context import ContextGraph` cuesta ~6 s de imports
# transitivos. A nivel de modulo eso hace que el handshake MCP se pase del
# timeout de sesion de ADK (5 s por defecto) y el toolset falle con
# "timed out waiting for the session to become ready". Por eso se importa
# DENTRO del cargador perezoso: el proceso responde al initialize en ~0.5 s y
# el coste se paga en la primera llamada a una herramienta.

# El nombre correcto es SEMANTICA_KG_PATH; se acepta SEMANTICA_GRAPH por
# compatibilidad con los .mcp.json que ya circulan.
GRAPH_PATH = (os.environ.get("SEMANTICA_KG_PATH")
              or os.environ.get("SEMANTICA_GRAPH")
              or "semantica_graph.json")

_write_lock = threading.Lock()
_load_lock = threading.Lock()
_log = mcp.log


# ── FIX 1 y 2: cargar de verdad (y perezosamente) ─────────────────────────
def _load_graph():
    from semantica.context import ContextGraph      # import caro, aqui dentro

    g = ContextGraph(advanced_analytics=True)
    if os.path.isfile(GRAPH_PATH):
        g.load_from_file(GRAPH_PATH)          # el metodo que si existe
        n = len(g.to_dict().get("nodes", []))
        _log.info("Grafo cargado desde %s (%d nodos)", GRAPH_PATH, n)
    else:
        _log.warning("No existe %s; se arranca con un grafo vacio", GRAPH_PATH)
    return g


def _get_graph():
    """Sustituye al `_get_graph` del paquete, que llama a un metodo inexistente."""
    if mcp._graph is None:
        with _load_lock:
            if mcp._graph is None:
                mcp._graph = _load_graph()
    return mcp._graph


mcp._get_graph = _get_graph


# ── FIX 3: persistir tras cada escritura ──────────────────────────────────
MUTATING = {"record_decision", "add_entity", "add_relationship", "run_reasoning"}


def _persist() -> None:
    with _write_lock:
        _get_graph().save_to_file(GRAPH_PATH)


def _wrap_persisting(tool: dict) -> None:
    original = tool["_handler"]

    def handler(args, _original=original, _name=tool["name"]):
        result = _original(args)
        if not (isinstance(result, dict) and result.get("error")):
            try:
                _persist()
                if isinstance(result, dict):
                    result["persisted_to"] = GRAPH_PATH
            except Exception as exc:                      # no romper la tool
                _log.warning("No se pudo persistir tras %s: %s", _name, exc)
                if isinstance(result, dict):
                    result["persist_error"] = str(exc)
        return result

    tool["_handler"] = handler


for _tool in mcp.TOOLS:
    if _tool["name"] in MUTATING:
        _wrap_persisting(_tool)


# ── Herramienta nueva: el gobierno se CONSULTA, no se recita ──────────────
def _tool_resolve_governance(args: dict) -> dict:
    """Resuelve taxonomia y gates aplicables para (servicio, intencion, entorno).

    Esta es la herramienta que permite quitar del prompt la lista y el orden de
    los gates. El agente pregunta, el grafo responde.
    """
    for field in ("service", "intent"):
        if not args.get(field):
            return {"error": f"missing required field: {field}"}

    q = GraphQuery(_get_graph())
    service, intent = args["service"], args["intent"]
    env = args.get("env", "dev")

    capability = q.capability_for_intent(intent)
    if not capability:
        known = [q.name(n["id"]) for n in q.nodes_of_type("Intent")]
        return {"error": f"intencion '{intent}' desconocida", "known_intents": known}

    if not q.node(service):
        known = [n["id"] for n in q.nodes_of_type("Service")]
        return {"error": f"servicio '{service}' no esta en el catalogo",
                "known_services": known}

    gates = q.gates_for(capability, env)
    return {
        "service": service,
        "capability": capability,
        "owner_domain": q.owner_domain(service),
        "handler_subagent": q.handler_subagent(capability),
        "env": env,
        "gate_count": len(gates),
        "gates": [
            {"policy": g["id"], "name": g["name"], "order": g["order"],
             "rule": g["expression"], "reads": g["reads"],
             "only_in_envs": g["env_scope"] or "todos"}
            for g in gates
        ],
        "instruction": ("Aplica EXACTAMENTE estos gates, en este orden. No añadas "
                        "ni omitas ninguno. Registra cada uno con record_decision."),
    }


def _tool_get_signals(args: dict) -> dict:
    """Sirve los VALORES de las señales para (servicio, entorno).

    Sin esta herramienta el agente LLM se inventa los hechos: decide por su
    cuenta si hay ventana de freeze o si el CI esta verde, y bloquea (o peor,
    despacha) por algo que nadie le dijo. Un gate evaluado contra datos
    inventados no gobierna nada.
    """
    for field in ("service", "env"):
        if not args.get(field):
            return {"error": f"missing required field: {field}"}
    try:
        detail = signals.explain(args["service"], args["env"])
    except FileNotFoundError:
        return {"error": "signals_source.json no encontrado"}
    return {
        "service": args["service"],
        "env": args["env"],
        "signals": detail["values"],
        "origin": detail["origin"],
        "non_spoofable": detail["non_spoofable"],
        "instruction": ("Usa EXACTAMENTE estos valores para evaluar las reglas de "
                        "los gates. No inventes ni infieras ninguno. Si una regla "
                        "lee una señal que no esta aqui, detente y dilo: no se "
                        "puede decidir sin el hecho."),
    }


mcp.TOOLS.append({
    "name": "get_signals",
    "description": (
        "Devuelve los valores reales de las señales de gobierno para un servicio y "
        "un entorno (estado de CI, escaneo de seguridad, ventana de freeze, "
        "aprobacion vigente, dominio del solicitante), junto con la capa de la que "
        "sale cada valor. Llamala SIEMPRE despues de resolve_governance y ANTES de "
        "evaluar ningun gate. Nunca supongas estos valores."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "service": {"type": "string", "description": "ID del servicio, p. ej. SVC-PAYMENTS-API"},
            "env":     {"type": "string", "description": "dev | staging | prod"},
        },
        "required": ["service", "env"],
    },
    "_handler": _tool_get_signals,
})


mcp.TOOLS.append({
    "name": "resolve_governance",
    "description": (
        "Resuelve, contra la ontologia del IDP, todo lo que hace falta para atender "
        "una peticion: la capacidad del ciclo de vida que realiza la intencion, el "
        "dominio dueño del servicio, el sub-agente que ejecuta esa capacidad, y la "
        "lista ORDENADA de gates de gobierno aplicables a esa capacidad en ese "
        "entorno, con su regla. Llamala SIEMPRE antes de actuar."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "service":  {"type": "string", "description": "ID del servicio, p. ej. SVC-PAYMENTS-API"},
            "intent":   {"type": "string", "description": "deploy | test | monitor | optimize | build | scaffold | operate | retire"},
            "env":      {"type": "string", "description": "dev | staging | prod (default: dev)"},
        },
        "required": ["service", "intent"],
    },
    "_handler": _tool_resolve_governance,
})


# ══════════════════════════════════════════════════════════════════════════
# Exploracion del grafo
#
# Sin estas, el agente sabe DECIDIR pero no sabe MIRAR: `resolve_governance`
# exige un ID como "SVC-PAYMENTS-API", pero el desarrollador escribe
# "despliega payments-api". El agente se quedaba pidiendo el ID al usuario, que
# es justo lo contrario de lo que promete un grafo de conocimiento.
# ══════════════════════════════════════════════════════════════════════════
def _summarize(q: GraphQuery, node_id: str) -> dict:
    node = q.node(node_id) or {}
    return {"id": node_id, "type": node.get("type"),
            "name": q.name(node_id), "description": node.get("content", "")}


def _tool_list_entities(args: dict) -> dict:
    """Lista lo que hay en el grafo, opcionalmente filtrado por tipo."""
    q = GraphQuery(_get_graph())
    wanted = args.get("type")
    types = sorted({(n.get("type") or "") for n in q._nodes.values()})
    if wanted and wanted not in types:
        return {"error": f"tipo '{wanted}' desconocido", "known_types": types}

    out = {}
    for t in ([wanted] if wanted else types):
        if t in ("decision", "category", "decision_maker"):
            continue        # runtime, no catalogo; se consulta con query_decisions
        out[t] = [_summarize(q, n["id"]) for n in q.nodes_of_type(t)]
    return {"types": types, "entities": out}


# Palabras que aparecen en casi todo el grafo y no discriminan nada.
_STOPWORDS = {
    "el", "la", "los", "las", "un", "una", "de", "del", "al", "a", "en", "y", "o",
    "que", "para", "por", "con", "es", "se", "su", "sus", "lo", "me", "mi", "the",
    "quiero", "necesito", "hay", "dame", "muestra", "cual", "cuales", "como",
    # negaciones y muletillas: "no" casaba con "No se despliega con
    # vulnerabilidades criticas" y colaba POL-SECSCAN en busquedas sin sentido
    "no", "si", "sin", "nada", "asi", "todo", "todos", "toda", "todas", "ni",
    "quien", "donde", "cuando", "porque", "sobre", "este", "esta", "estos",
}


def _tokens(text: str) -> set[str]:
    cleaned = "".join(c if c.isalnum() or c in "-_" else " " for c in text.lower())
    return {w for w in cleaned.split() if w and w not in _STOPWORDS and len(w) > 1}


def _tool_find_entity(args: dict) -> dict:
    """Resuelve texto libre a entidades del grafo. Devuelve candidatos ordenados.

    El scoring pesa cada termino por lo RARO que sea en el grafo (idf). Sin eso,
    "el servicio de pagos" puntuaba igual a SVC-PAYMENTS-API que a INT-SCAFFOLD,
    porque la palabra "servicio" aparece en la descripcion de medio catalogo.
    """
    raw = str(args.get("query", "")).strip()
    if not raw:
        return {"error": "missing required field: query"}
    q = GraphQuery(_get_graph())
    wanted = args.get("type")
    query_low = raw.lower()
    q_tokens = _tokens(raw)

    # Corpus para el idf: todos los nodos del catalogo (no las decisiones).
    corpus = {}
    for node in q._nodes.values():
        nid, ntype = node["id"], node.get("type")
        if ntype in ("decision", "category", "decision_maker"):
            continue
        corpus[nid] = _tokens(f"{nid} {q.props(nid).get('name', '')} {node.get('content') or ''}")

    import math
    n_docs = max(len(corpus), 1)
    df = {}
    for toks in corpus.values():
        for t in toks:
            df[t] = df.get(t, 0) + 1
    def idf(t: str) -> float:
        return math.log(1 + n_docs / (1 + df.get(t, 0)))

    total_idf = sum(idf(t) for t in q_tokens) or 1.0

    scored = []
    for nid, doc_tokens in corpus.items():
        if wanted and q._nodes[nid].get("type") != wanted:
            continue
        name_low = str(q.props(nid).get("name", "")).lower()
        id_low = nid.lower()

        if query_low in (name_low, id_low):
            score = 1.0                                   # coincidencia exacta
        elif name_low and (name_low in query_low or query_low in name_low):
            score = 0.9                                   # el nombre esta contenido
        elif query_low in id_low:
            score = 0.85
        else:
            hit = q_tokens & doc_tokens
            score = 0.75 * (sum(idf(t) for t in hit) / total_idf) if hit else 0.0
        # Por debajo de este piso la coincidencia es ruido lexico, no una
        # candidata: mejor devolver vacio que una lista que aparenta señal.
        if score > 0.15:
            scored.append((score, nid))

    scored.sort(key=lambda x: (-x[0], x[1]))
    top = [{**_summarize(q, nid), "score": round(sc, 2)} for sc, nid in scored[:8]]
    result = {"query": raw, "match_count": len(top), "candidates": top}
    # Umbral de confianza: una coincidencia debil presentada como certeza es peor
    # que no encontrar nada, porque el agente actua sobre la entidad equivocada.
    WEAK = 0.40
    if not top:
        result["confidence"] = "none"
        result["instruction"] = ("Nada coincide. Usa `list_entities` para ver el "
                                 "catalogo y pide al usuario que elija; no inventes IDs.")
    elif top[0]["score"] < WEAK:
        result["confidence"] = "low"
        result["instruction"] = (
            "Ninguna coincidencia es fiable (la mejor puntua "
            f"{top[0]['score']}). NO uses estos candidatos como si fueran la "
            "respuesta. Si la pregunta es de catalogo ('que servicios hay', "
            "'quien maneja los despliegues'), usa `list_entities` o "
            "`describe_entity` sobre la capacidad o el dominio. Si buscabas una "
            "entidad concreta, pide al usuario que la nombre mejor.")
    elif (len(top) > 1 and top[0]["score"] < 1.0
          and top[0]["score"] - top[1]["score"] < 0.15):
        # Una coincidencia EXACTA de nombre o id (1.0) gana siempre: que otra
        # entidad comparta la raiz del nombre no la hace ambigua.
        result["confidence"] = "ambiguous"
        result["instruction"] = ("Hay mas de un candidato con puntuacion similar. "
                                 "Pide al usuario que desambigue antes de actuar.")
    else:
        result["confidence"] = "high"
        result["instruction"] = f"Usa '{top[0]['id']}' como identificador."
    return result


def _tool_describe_entity(args: dict) -> dict:
    """Todo lo que el grafo sabe de una entidad, en AMBAS direcciones."""
    node_id = args.get("id")
    if not node_id:
        return {"error": "missing required field: id"}
    q = GraphQuery(_get_graph())
    if not q.node(node_id):
        return {"error": f"'{node_id}' no existe en el grafo",
                "hint": "usa find_entity para resolver un nombre a un ID"}

    exp = q.explain(node_id)
    RUNTIME = {"decision", "category", "decision_maker"}

    def side(pairs, key):
        rel, runtime_n = [], 0
        for etype, other in pairs:
            otype = (q.node(other) or {}).get("type")
            if otype in RUNTIME:
                runtime_n += 1      # rastro de ejecucion, no estructura
                continue
            rel.append({"edge": etype, key: other,
                        f"{key}_name": q.name(other), f"{key}_type": otype})
        return rel, runtime_n

    # Salientes y entrantes: el grafo declara cada hecho una sola vez, asi que el
    # dueño de un servicio esta en las ENTRANTES (Domain-[OWNS]->Service).
    outgoing, out_rt = side(exp["outgoing"], "to")
    incoming, in_rt = side(exp["incoming"], "from")
    return {
        **_summarize(q, node_id),
        "properties": q.props(node_id),
        "outgoing": outgoing,
        "incoming": incoming,
        "related_decisions": out_rt + in_rt,
        "note": ("Las decisiones que tocan esta entidad no se listan aqui; "
                 "consultalas con query_decisions.") if (out_rt + in_rt) else None,
    }


mcp.TOOLS.extend([
    {
        "name": "find_entity",
        "description": (
            "Resuelve texto libre (lo que escribio el desarrollador: 'payments-api', "
            "'el servicio de pagos', 'checkout') a entidades reales del grafo, "
            "ordenadas por confianza. Llamala SIEMPRE primero para obtener el ID de "
            "un servicio antes de resolve_governance. Nunca inventes ni adivines un ID."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Texto libre a resolver"},
                "type": {"type": "string", "description": "Opcional: Service, Domain, Capability, Policy, SubAgent, Intent, Signal, Agent"},
            },
            "required": ["query"],
        },
        "_handler": _tool_find_entity,
    },
    {
        "name": "list_entities",
        "description": (
            "Lista el catalogo del grafo: servicios desplegables, dominios de "
            "ingenieria, capacidades del ciclo de vida, politicas de gobierno, "
            "sub-agentes y señales. Usala cuando no sepas que existe o cuando "
            "tengas que ofrecerle opciones al usuario."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "description": "Opcional: filtra por tipo de entidad"},
            },
        },
        "_handler": _tool_list_entities,
    },
    {
        "name": "describe_entity",
        "description": (
            "Devuelve todo lo que el grafo sabe de una entidad: sus propiedades y sus "
            "relaciones en AMBAS direcciones (que apunta a ella y a que apunta). Con "
            "esto respondes 'quien es dueño de este servicio', 'que politicas gobiernan "
            "esta capacidad' o 'que capacidades maneja este sub-agente'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "ID exacto de la entidad"}},
            "required": ["id"],
        },
        "_handler": _tool_describe_entity,
    },
])


def main() -> None:
    _log.info("Shim MCP de Semantica sobre %s — %d herramientas (carga perezosa)",
              GRAPH_PATH, len(mcp.TOOLS))
    mcp._run_stdio()


if __name__ == "__main__":
    main()
