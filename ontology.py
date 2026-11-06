"""
ontology.py  —  CAPA 1 de 3: la ONTOLOGIA (T-Box)
--------------------------------------------------
El MODELO: que tipos de cosa existen, que relaciones son legales entre ellos, y
que reglas de gobierno hay. NO contiene hechos de tu empresa (esos van en
`catalog.py`) ni decisiones de ejecucion (esas las escribe `runtime.py`).

Ciclo de vida:  humano · versionado en git · revisado · cambia por trimestre.

Que se arreglo aqui respecto a la version anterior del repo:

1. EL VOCABULARIO ESTA DECLARADO. Antes los tipos de arista eran strings sueltos
   en llamadas a `add_edge`. Un dedazo (`OWNED_BY` en vez de `OWNS`) creaba un
   tipo de relacion nuevo en silencio. Ahora `EDGE_TYPES` declara dominio y
   rango de cada arista y `validate()` rechaza el grafo si algo no cuadra.

2. LAS POLITICAS SE ENGANCHAN A LO QUE GOBIERNAN. Antes las 5 politicas colgaban
   todas de `AGENT-ORCH` con `GOVERNED_BY`, asi que el grafo NO podia responder
   "que gates aplican a desplegar a prod" y el orden de los gates vivia escrito
   en el prompt del agente. Ahora: Policy-[APPLIES_TO]->Capability, con
   `env_scope` y `gate_order` como propiedades. El agente los DESCUBRE.

3. LA REGLA ES DATO. Cada politica lleva su condicion como expresion booleana
   (`expression`) evaluada contra las señales de la peticion. Añadir un gate no
   toca ni una linea de Python.

4. SIN ARISTAS INVERSAS A MANO. Cada hecho se declara una vez; `graph_query`
   resuelve las dos direcciones.
"""
from __future__ import annotations

# ══════════════════════════════════════════════════════════════════════════
# 1) TIPOS DE NODO
# ══════════════════════════════════════════════════════════════════════════
NODE_TYPES = {
    "Domain":     "Dominio de ingenieria: la unidad de propiedad de la empresa.",
    "Capability": "Etapa del ciclo de vida de aplicaciones (Plan..Retire).",
    "Service":    "Un servicio desplegable, propiedad de exactamente un dominio.",
    "Agent":      "Agente orquestador de la plataforma.",
    "SubAgent":   "Agente especializado que ejecuta una o mas capacidades.",
    "Policy":     "Regla de gobierno que condiciona una capacidad (un gate).",
    "Signal":     "Entrada observable que una politica evalua.",
    "Intent":     "Intencion expresada por el desarrollador en su peticion.",
}

# ══════════════════════════════════════════════════════════════════════════
# 2) VOCABULARIO DE ARISTAS  (dominio -> rango)
#    Cada hecho se declara UNA sola vez, en la direccion natural.
# ══════════════════════════════════════════════════════════════════════════
EDGE_TYPES = {
    "OWNS":       ("Domain",   "Service",    "El dominio es dueño del servicio."),
    "AT_STAGE":   ("Service",  "Capability", "Etapa actual del servicio en el ciclo de vida."),
    "NEXT_STAGE": ("Capability", "Capability", "Sucesion de etapas del ciclo de vida."),
    "ORCHESTRATES": ("Agent",  "SubAgent",   "El orquestador coordina al sub-agente."),
    "HANDLES":    ("SubAgent", "Capability", "El sub-agente ejecuta esa capacidad."),
    "REALIZES":   ("Intent",   "Capability", "La intencion se resuelve en esa capacidad."),
    "APPLIES_TO": ("Policy",   "Capability", "La politica gobierna esa capacidad."),
    "EVALUATES":  ("Policy",   "Signal",     "La politica lee esa señal."),
    "FEEDS":      ("Signal",   "Agent",      "La señal alimenta al orquestador."),
}

# ══════════════════════════════════════════════════════════════════════════
# 3) CAPACIDADES del ciclo de vida. Son MODELO, no inventario.
# ══════════════════════════════════════════════════════════════════════════
CAPABILITIES = [
    ("C-PLAN",     "Plan",     1, "Definir alcance, scaffolding y diseño del servicio"),
    ("C-BUILD",    "Build",    2, "Construir y empaquetar el servicio"),
    ("C-TEST",     "Test",     3, "CI: pruebas unitarias, integracion y calidad"),
    ("C-DEPLOY",   "Deploy",   4, "Liberar el servicio a un entorno (dev/stg/prod)"),
    ("C-OPERATE",  "Operate",  5, "Operar el servicio en produccion (on-call, SRE)"),
    ("C-MONITOR",  "Monitor",  6, "Observabilidad: SLOs, alertas, trazas"),
    ("C-OPTIMIZE", "Optimize", 7, "Optimizar costo y rendimiento (FinOps)"),
    ("C-RETIRE",   "Retire",   8, "Decomisionar servicios y limpiar recursos"),
]

# ══════════════════════════════════════════════════════════════════════════
# 4) INTENCIONES -> CAPACIDAD  (antes: dict INTENT_TO_CAPABILITY en Python)
# ══════════════════════════════════════════════════════════════════════════
INTENTS = [
    ("INT-SCAFFOLD", "scaffold", "C-PLAN",     "Crear el andamiaje de un servicio nuevo"),
    ("INT-BUILD",    "build",    "C-BUILD",    "Compilar y empaquetar"),
    ("INT-TEST",     "test",     "C-TEST",     "Correr el pipeline de CI"),
    ("INT-DEPLOY",   "deploy",   "C-DEPLOY",   "Liberar a un entorno"),
    ("INT-OPERATE",  "operate",  "C-OPERATE",  "Operacion y on-call"),
    ("INT-MONITOR",  "monitor",  "C-MONITOR",  "Investigar comportamiento en produccion"),
    ("INT-OPTIMIZE", "optimize", "C-OPTIMIZE", "Reducir costo o mejorar rendimiento"),
    ("INT-RETIRE",   "retire",   "C-RETIRE",   "Decomisionar"),
]

# ══════════════════════════════════════════════════════════════════════════
# 5) SEÑALES que las politicas leen
# ══════════════════════════════════════════════════════════════════════════
SIGNALS = [
    ("SIG-REQUEST", "Developer Request",  "Peticion del desarrollador (lenguaje natural)"),
    ("SIG-OWNER",   "Requester Identity", "Dominio al que pertenece quien pide"),
    ("SIG-CI",      "CI Status",          "Estado del pipeline de CI del servicio"),
    ("SIG-SEC",     "Security Scan",      "Resultado del escaneo de seguridad (SAST/deps)"),
    ("SIG-ENV",     "Target Environment", "Entorno destino: dev / staging / prod"),
    ("SIG-FREEZE",  "Change Freeze",      "Si hay ventana de congelamiento activa"),
    ("SIG-APPROVAL","Change Approval",    "Aprobacion vigente de un responsable"),
    ("SIG-SLO",     "SLO Error Budget",   "Si queda error budget del SLO del servicio"),
]

# ══════════════════════════════════════════════════════════════════════════
# 6) POLITICAS DE GOBIERNO
#
#    `applies_to`  -> aristas APPLIES_TO: QUE capacidad gobierna
#    `env_scope`   -> [] = todos los entornos; ['prod'] = solo produccion
#    `gate_order`  -> el ORDEN de evaluacion vive aqui, no en el prompt
#    `expression`  -> la REGLA, como dato evaluable contra las señales
#    `reads`       -> aristas EVALUATES: que señales necesita
# ══════════════════════════════════════════════════════════════════════════
POLICIES = [
    {
        "id": "POL-OWNERSHIP",
        "name": "Domain Ownership Gate",
        "content": "Solo el dominio dueño puede accionar sobre su servicio",
        "gate_order": 10,
        "env_scope": [],
        "expression": "requester_domain == owner_domain",
        "applies_to": ["C-PLAN", "C-BUILD", "C-TEST", "C-DEPLOY",
                       "C-OPERATE", "C-MONITOR", "C-OPTIMIZE", "C-RETIRE"],
        "reads": ["SIG-OWNER", "SIG-REQUEST"],
    },
    {
        "id": "POL-TESTS",
        "name": "CI Tests Gate",
        "content": "No se despliega sin CI en verde",
        "gate_order": 20,
        "env_scope": [],
        "expression": "ci_status == 'passed'",
        "applies_to": ["C-DEPLOY"],
        "reads": ["SIG-CI"],
    },
    {
        "id": "POL-SECSCAN",
        "name": "Security Scan Gate",
        "content": "No se despliega con vulnerabilidades criticas",
        "gate_order": 30,
        "env_scope": [],
        "expression": "security == 'clean'",
        "applies_to": ["C-DEPLOY"],
        "reads": ["SIG-SEC"],
    },
    {
        "id": "POL-FREEZE",
        "name": "Change Freeze Window",
        "content": "Sin cambios en ventana de congelamiento, salvo aprobacion explicita",
        "gate_order": 40,
        "env_scope": ["prod"],
        "expression": "not in_freeze_window or approved",
        "applies_to": ["C-DEPLOY", "C-RETIRE"],
        "reads": ["SIG-FREEZE", "SIG-APPROVAL", "SIG-ENV"],
    },
    {
        "id": "POL-APPROVAL",
        "name": "Prod Approval Gate",
        "content": "Prod requiere aprobacion de un responsable",
        "gate_order": 50,
        "env_scope": ["prod"],
        "expression": "approved",
        "applies_to": ["C-DEPLOY", "C-RETIRE"],
        "reads": ["SIG-APPROVAL", "SIG-ENV"],
    },
    
]


# ══════════════════════════════════════════════════════════════════════════
# INSTALACION EN EL GRAFO
# ══════════════════════════════════════════════════════════════════════════
def install(g) -> dict:
    """Escribe la ontologia (T-Box) en el grafo. Idempotente."""
    counts = {"capabilities": 0, "intents": 0, "signals": 0, "policies": 0, "edges": 0}

    for cid, name, order, desc in CAPABILITIES:
        g.add_node(cid, "Capability", content=desc, name=name,
                   lifecycle_order=order, layer="ontology")
        counts["capabilities"] += 1
    for (a, *_), (b, *_) in zip(CAPABILITIES, CAPABILITIES[1:]):
        g.add_edge(a, b, edge_type="NEXT_STAGE"); counts["edges"] += 1

    for iid, verb, cap, desc in INTENTS:
        g.add_node(iid, "Intent", content=desc, name=verb, layer="ontology")
        g.add_edge(iid, cap, edge_type="REALIZES"); counts["edges"] += 1
        counts["intents"] += 1

    for sid, name, desc in SIGNALS:
        g.add_node(sid, "Signal", content=desc, name=name, layer="ontology")
        counts["signals"] += 1

    for pol in POLICIES:
        g.add_node(pol["id"], "Policy", content=pol["content"],
                   name=pol["name"],
                   gate_order=pol["gate_order"],
                   env_scope=pol["env_scope"],
                   expression=pol["expression"],
                   layer="ontology")
        counts["policies"] += 1
        for cap in pol["applies_to"]:
            g.add_edge(pol["id"], cap, edge_type="APPLIES_TO"); counts["edges"] += 1
        for sig in pol["reads"]:
            g.add_edge(pol["id"], sig, edge_type="EVALUATES"); counts["edges"] += 1

    return counts


# ══════════════════════════════════════════════════════════════════════════
# VALIDACION  (esto es lo que impide que un dedazo cree un tipo nuevo)
# ══════════════════════════════════════════════════════════════════════════
class OntologyViolation(Exception):
    pass


def validate(g, strict: bool = True) -> list[str]:
    """Comprueba que TODO nodo y arista del grafo respeta el T-Box.

    Ignora los nodos que Semantica genera sola al registrar decisiones
    (`decision`, `category`, `decision_maker`) y sus aristas: son runtime,
    no modelo.
    """
    runtime_node_types = {"decision", "category", "decision_maker"}
    runtime_edge_types = {"involves", "belongs_to", "made_by",
                          "CAUSED", "INFLUENCED", "PRECEDENT_FOR"}

    data = g.to_dict()
    nodes = {n["id"]: n for n in data.get("nodes", [])}
    problems: list[str] = []

    for nid, n in nodes.items():
        ntype = n.get("type")
        if ntype in runtime_node_types:
            continue
        if ntype not in NODE_TYPES:
            problems.append(f"nodo '{nid}': tipo desconocido '{ntype}'")

    for e in data.get("edges", []):
        etype = e.get("type")
        if etype in runtime_edge_types:
            continue
        if etype not in EDGE_TYPES:
            problems.append(f"arista '{e.get('source')} -{etype}-> {e.get('target')}': "
                            f"tipo fuera del vocabulario")
            continue
        exp_src, exp_tgt, _ = EDGE_TYPES[etype]
        src_t = nodes.get(e.get("source"), {}).get("type")
        tgt_t = nodes.get(e.get("target"), {}).get("type")
        if src_t in runtime_node_types or tgt_t in runtime_node_types:
            continue
        if src_t != exp_src:
            problems.append(f"arista {etype}: origen '{e.get('source')}' es "
                            f"{src_t}, se esperaba {exp_src}")
        if tgt_t != exp_tgt:
            problems.append(f"arista {etype}: destino '{e.get('target')}' es "
                            f"{tgt_t}, se esperaba {exp_tgt}")

    if problems and strict:
        raise OntologyViolation(
            f"{len(problems)} violacion(es) del T-Box:\n  - " + "\n  - ".join(problems)
        )
    return problems
