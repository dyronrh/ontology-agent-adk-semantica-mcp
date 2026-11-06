"""
compare_ontologia.py
--------------------
Prueba A/B sobre las MISMAS 7 peticiones:

  A) SIN ontologia  -> ruteo por SIMILITUD de texto, sin gates, sin auditoria.
                       (un router LLM/RAG "pelon").
  B) CON ontologia  -> `GovernedOrchestrator` resuelve contra el grafo tipado y
                       aplica los gates que el grafo declare.

Mide contra la verdad-terreno: ruteo correcto, gobierno correcto, auditabilidad.

    python seed_semantica.py
    python compare_ontologia.py

Cambio respecto a la version anterior: el brazo B ya NO reimplementa los gates.
Antes este fichero tenia su propio dict `gates = {ownership:..., ci:..., ...}`,
duplicando la logica que ya estaba en la demo y en el prompt del agente — tres
copias de la misma regla, que se desincronizan a la primera politica nueva.
Ahora llama a `runtime.GovernedOrchestrator`, el mismo que usan la demo y el
agente ADK. Una regla, un sitio.
"""
from difflib import SequenceMatcher

from semantica.context import ContextGraph

from runtime import GovernedOrchestrator

GRAPH_PATH = "semantica_graph.json"

# ---------------------------------------------------------------------------
# Verdad-terreno
# ---------------------------------------------------------------------------
CASES = [
    dict(text="desplegar payments-api a produccion",
         service="SVC-PAYMENTS-API", intent="deploy", requester_domain="D-PAYMENTS",
         ci_status="passed", security="clean", in_freeze_window=False, approved=True, slo_ok=True, env="prod",
         gt_route="SA-DEPLOY", gt_verdict="DISPATCH",
         note="camino feliz: ambos deberian rutear a deploy"),

    dict(text="desplegar payments-api a produccion (urgente)",
         service="SVC-PAYMENTS-API", intent="deploy", requester_domain="D-PAYMENTS",
         ci_status="failed", security="clean", in_freeze_window=False, approved=True, slo_ok=True, env="prod",
         gt_route="SA-DEPLOY", gt_verdict="BLOCK",
         note="CI EN ROJO -> debe bloquear; el baseline no tiene gate y despliega"),

    dict(text="desplegar payments-api a prod desde el equipo de frontend",
         service="SVC-PAYMENTS-API", intent="deploy", requester_domain="D-FRONTEND",
         ci_status="passed", security="clean", in_freeze_window=False, approved=True, slo_ok=True, env="prod",
         gt_route="SA-DEPLOY", gt_verdict="BLOCK",
         note="NO es el dominio dueño -> POL-OWNERSHIP bloquea; baseline no lo ve"),

    dict(text="hay que probar el pipeline de auth antes de nada",
         service="SVC-AUTH", intent="test", requester_domain="D-IDENTITY",
         ci_status="passed", security="clean", in_freeze_window=False, approved=True, slo_ok=True, env="staging",
         gt_route="SA-TEST", gt_verdict="DISPATCH",
         note="intent=test; la palabra 'pipeline' puede confundir al baseline"),

    dict(text="revisar por que checkout-web se cae de madrugada",
         service="SVC-CHECKOUT-WEB", intent="monitor", requester_domain="D-FRONTEND",
         ci_status="passed", security="clean", in_freeze_window=False, approved=True, slo_ok=True, env="prod",
         gt_route="SA-SRE", gt_verdict="DISPATCH",
         note="intent=monitor; sin overlap lexico con 'observabilidad'"),

    dict(text="bajar la factura de nube del feature-store",
         service="SVC-FEATURE-STORE", intent="optimize", requester_domain="D-DATA",
         ci_status="passed", security="clean", in_freeze_window=False, approved=True, slo_ok=True, env="prod",
         gt_route="SA-COST", gt_verdict="DISPATCH",
         note="intent=optimize (FinOps); baseline puede irse a deploy/scaffold"),

    dict(text="desplegar auth-service a prod en plena ventana de freeze",
         service="SVC-AUTH", intent="deploy", requester_domain="D-IDENTITY",
         ci_status="passed", security="clean", in_freeze_window=True, approved=False, slo_ok=True, env="prod",
         gt_route="SA-DEPLOY", gt_verdict="BLOCK",
         note="freeze activo y sin aprobacion -> bloquear; baseline lo ignora"),
]


# ===========================================================================
# Carga del grafo (una vez, compartido por los dos brazos)
# ===========================================================================
g = ContextGraph(advanced_analytics=True)
g.load_from_file(GRAPH_PATH)
orch = GovernedOrchestrator(g)

SUBAGENT_DESC = {
    n["id"]: (n.get("properties", {}).get("name", "") + " " + (n.get("content") or ""))
    for n in g.to_dict()["nodes"] if n.get("type") == "SubAgent"
}


# ===========================================================================
# A) SIN ONTOLOGIA — similitud de texto, sin gates, sin auditoria
# ===========================================================================
def router_sin_ontologia(case):
    text = case["text"].lower()
    best, best_score = None, -1.0
    for said, desc in SUBAGENT_DESC.items():
        score = SequenceMatcher(None, text, desc.lower()).ratio()
        score += 0.1 * len(set(text.split()) & set(desc.lower().split()))
        if score > best_score:
            best, best_score = said, score
    return {"route": best, "verdict": "DISPATCH", "auditable": False,
            "reason": None, "gates": 0}


# ===========================================================================
# B) CON ONTOLOGIA — el mismo orquestador gobernado que usa el agente ADK
# ===========================================================================
def router_con_ontologia(case):
    res = orch.handle(case)
    return {"route": res["handler_subagent"], "verdict": res["verdict"],
            "auditable": True, "reason": res["reason"],
            "gates": len(res["gate_results"]),
            "decision_id": res["decisions"][-1] if res["decisions"] else None}


# ===========================================================================
def run(name, router, show_gates=False):
    print("=" * 96)
    print(f"ORQUESTADOR {name}")
    print("=" * 96)
    print(f"{'#':<2} {'caso':<46} {'':<4} {'ruteo':<10} {'gobierno':<10} "
          f"{'gates':<6} {'auditable':<9}")
    print("-" * 96)
    r_ok = gov_ok_n = aud = 0
    for i, case in enumerate(CASES, 1):
        res = router(case)
        route_ok = res["route"] == case["gt_route"]
        gov_ok = res["verdict"] == case["gt_verdict"]
        r_ok += route_ok; gov_ok_n += gov_ok; aud += res["auditable"]
        tag = "OK " if (route_ok and gov_ok) else "XX "
        print(f"{i:<2} {case['text'][:46]:<46} {tag:<4} "
              f"{('correcto' if route_ok else 'MAL'):<10} "
              f"{(res['verdict'] + ('' if gov_ok else '!')):<10} "
              f"{res['gates']:<6} {('si' if res['auditable'] else 'no'):<9}")
        if show_gates and res.get("reason") and res["verdict"] == "BLOCK":
            print(f"   -> {res['reason']}")
    n = len(CASES)
    print("-" * 96)
    print(f"   Ruteo correcto: {r_ok}/{n}   |   Gobierno correcto: {gov_ok_n}/{n}   |   "
          f"Auditables: {aud}/{n}")
    return r_ok, gov_ok_n, aud


a = run("A — SIN ontologia (similitud de texto)", router_sin_ontologia)
print()
b = run("B — CON ontologia (grafo tipado + gates consultados + auditoria)",
        router_con_ontologia, show_gates=True)

n = len(CASES)
print()
print("=" * 96)
print("RESUMEN")
print("=" * 96)
print(f"  SIN ontologia:  ruteo {a[0]}/{n} · gobierno {a[1]}/{n} · auditable {a[2]}/{n}")
print(f"  CON ontologia:  ruteo {b[0]}/{n} · gobierno {b[1]}/{n} · auditable {b[2]}/{n}")
print()
print("Lectura para el video:")
print("  - Sin ontologia el gobierno se cae: despliega con CI en rojo, sin ser dueño")
print("    o en pleno freeze, porque solo mira parecido de texto (no hechos).")
print("  - Con ontologia cada decision esta anclada a la taxonomia y a los gates que")
print("    el GRAFO declara para esa capacidad y ese entorno — fijate en la columna")
print("    'gates': el caso 4 (test/staging) pasa por 1, los deploys a prod por 6.")
print("  - Y queda trazable: 'por que bloqueaste' se responde con la cadena causal.")
