"""
test_ontologia.py
-----------------
Pruebas de lo que el rediseño afirma. Sin dependencias: `python test_ontologia.py`.

Cubre:
  1. El T-Box rechaza tipos de arista fuera del vocabulario (el "dedazo").
  2. El T-Box rechaza aristas con dominio/rango equivocados.
  3. Las inversas ya no se declaran a mano: una arista, dos direcciones.
  4. Los gates dependen del ENTORNO y salen del grafo, no del codigo.
  5. El evaluador de politicas rechaza cualquier cosa que no sea booleana.
  6. `ownership` se evalua contra el dueño DEL GRAFO, no contra lo que diga
     el solicitante (no se puede falsear pidiendo con otro dominio).
  7. La verdad-terreno de la prueba A/B se reproduce entera.
  8. Una herramienta alucinada por el LLM vuelve como error, no como crash.
"""
import sys
import traceback

from semantica.context import ContextGraph

import catalog
import ontology
import signals
from graph_query import GraphQuery
from runtime import GovernedOrchestrator, PolicyExpressionError, evaluate_policy

PASS, FAIL = [], []


def check(name, fn):
    try:
        fn()
        PASS.append(name)
        print(f"  ok   {name}")
    except Exception as exc:
        FAIL.append((name, exc))
        print(f"  FAIL {name}\n       {type(exc).__name__}: {exc}")


def fresh():
    g = ContextGraph(advanced_analytics=True)
    ontology.install(g)
    catalog.sync(g)
    return g


def with_slo(g):
    """Asegura POL-SLO en el grafo, este o no en `ontology.POLICIES`.

    La demo (DEMO_ADK.md, paso 7) la añade en vivo, asi que `ontology.py` puede
    estar con o sin ella; las pruebas que cuentan 6 gates no deben depender de eso.
    """
    if not g.has_node("POL-SLO"):
        g.add_node("POL-SLO", "Policy", content="No desplegar con el error budget agotado",
                   name="SLO Budget Gate", gate_order=25, env_scope=["prod"],
                   expression="slo_ok", layer="ontology")
        g.add_edge("POL-SLO", "C-DEPLOY", edge_type="APPLIES_TO")
        g.add_edge("POL-SLO", "SIG-SLO", edge_type="EVALUATES")
    return g


# ── 1 y 2: el T-Box de verdad valida ──────────────────────────────────────
def t_edge_type_desconocido():
    g = fresh()
    g.add_edge("D-PAYMENTS", "SVC-PAYMENTS-API", edge_type="OWNED_BY")  # dedazo
    problems = ontology.validate(g, strict=False)
    assert any("OWNED_BY" in p for p in problems), problems
    try:
        ontology.validate(g, strict=True)
        raise AssertionError("validate(strict=True) deberia haber lanzado")
    except ontology.OntologyViolation:
        pass


def t_dominio_rango():
    g = fresh()
    # OWNS es Domain -> Service; esto lo invierte
    g.add_edge("SVC-PAYMENTS-API", "D-PAYMENTS", edge_type="OWNS")
    problems = ontology.validate(g, strict=False)
    assert any("OWNS" in p and "origen" in p for p in problems), problems


def t_grafo_limpio_valida():
    assert ontology.validate(fresh(), strict=False) == []


# ── 3: sin inversas a mano ────────────────────────────────────────────────
def t_una_arista_dos_direcciones():
    g = fresh()
    data = g.to_dict()
    tipos = {e["type"] for e in data["edges"]}
    assert "IN_DOMAIN" not in tipos, "reaparecio la inversa hecha a mano"
    assert "HANDLED_BY" not in tipos, "reaparecio la inversa hecha a mano"

    q = GraphQuery(g)
    # el dueño se resuelve por la arista ENTRANTE, sin duplicarla
    assert q.owner_domain("SVC-PAYMENTS-API") == "D-PAYMENTS"
    assert q.handler_subagent("C-DEPLOY") == "SA-DEPLOY"
    # y la API cruda de Semantica sigue siendo direccional (por eso hace falta)
    assert g.get_neighbors("SVC-PAYMENTS-API", hops=1) is not None


# ── 4: los gates salen del grafo y dependen del entorno ───────────────────
def t_gates_por_entorno():
    q = GraphQuery(with_slo(fresh()))
    prod = [g["id"] for g in q.gates_for("C-DEPLOY", "prod")]
    stg = [g["id"] for g in q.gates_for("C-DEPLOY", "staging")]
    assert prod == ["POL-OWNERSHIP", "POL-TESTS", "POL-SLO", "POL-SECSCAN",
                    "POL-FREEZE", "POL-APPROVAL"], prod
    assert stg == ["POL-OWNERSHIP", "POL-TESTS", "POL-SECSCAN"], stg
    assert len(prod) > len(stg)


def t_gates_ordenados():
    q = GraphQuery(fresh())
    orders = [g["order"] for g in q.gates_for("C-DEPLOY", "prod")]
    assert orders == sorted(orders), orders


def t_politica_nueva_sin_tocar_codigo():
    """Añadir una politica al grafo cambia el comportamiento del orquestador."""
    g = fresh()
    g.add_node("POL-SLO", "Policy", content="No desplegar con el SLO en rojo",
               name="SLO Budget Gate", gate_order=25, env_scope=["prod"],
               expression="slo_ok", layer="ontology")
    g.add_edge("POL-SLO", "C-DEPLOY", edge_type="APPLIES_TO")
    assert ontology.validate(g, strict=False) == []
    ids = [x["id"] for x in GraphQuery(g).gates_for("C-DEPLOY", "prod")]
    # gate_order=25 la coloca entre POL-TESTS (20) y POL-SECSCAN (30):
    # la posicion la decide el dato del grafo, no el orden de insercion.
    assert ids == ["POL-OWNERSHIP", "POL-TESTS", "POL-SLO", "POL-SECSCAN",
                   "POL-FREEZE", "POL-APPROVAL"], ids


def t_intent_desde_el_grafo():
    q = GraphQuery(fresh())
    assert q.capability_for_intent("deploy") == "C-DEPLOY"
    assert q.capability_for_intent("optimize") == "C-OPTIMIZE"
    assert q.capability_for_intent("inventada") is None


# ── 5: el evaluador de politicas es seguro ────────────────────────────────
def t_evaluador_basico():
    assert evaluate_policy("ci_status == 'passed'", {"ci_status": "passed"}) is True
    assert evaluate_policy("ci_status == 'passed'", {"ci_status": "failed"}) is False
    assert evaluate_policy("not in_freeze_window or approved",
                           {"in_freeze_window": True, "approved": True}) is True
    assert evaluate_policy("not in_freeze_window or approved",
                           {"in_freeze_window": True, "approved": False}) is False


def t_evaluador_rechaza_codigo():
    for maligna in ("__import__('os').system('id')",
                    "open('/etc/passwd').read()",
                    "ci_status.upper() == 'PASSED'",
                    "[x for x in range(10)]"):
        try:
            evaluate_policy(maligna, {"ci_status": "passed"})
            raise AssertionError(f"deberia haber rechazado: {maligna}")
        except PolicyExpressionError:
            pass


def t_evaluador_exige_señales():
    try:
        evaluate_policy("slo_ok", {"ci_status": "passed"})
        raise AssertionError("deberia exigir la señal que falta")
    except PolicyExpressionError as exc:
        assert "slo_ok" in str(exc)


# ── 6: ownership no se puede falsear ──────────────────────────────────────
def t_ownership_usa_el_grafo():
    orch = GovernedOrchestrator(fresh())
    base = dict(service="SVC-PAYMENTS-API", intent="deploy", env="prod",
                ci_status="passed", security="clean",
                in_freeze_window=False, approved=True, slo_ok=True)
    ok = orch.handle({**base, "requester_domain": "D-PAYMENTS"}, record=False)
    assert ok["verdict"] == "DISPATCH", ok
    assert ok["owner_domain"] == "D-PAYMENTS"

    falso = orch.handle({**base, "requester_domain": "D-FRONTEND"}, record=False)
    assert falso["verdict"] == "BLOCK", falso
    assert falso["failed_gates"] == ["POL-OWNERSHIP"], falso["failed_gates"]
    # el dueño sigue saliendo del grafo, no de la peticion
    assert falso["owner_domain"] == "D-PAYMENTS"


# ── 7: la verdad-terreno de la A/B ────────────────────────────────────────
GROUND_TRUTH = [
    ("SVC-PAYMENTS-API", "deploy",  "D-PAYMENTS", "passed", "clean", False, True,  True,  "prod",    "SA-DEPLOY", "DISPATCH"),
    ("SVC-PAYMENTS-API", "deploy",  "D-PAYMENTS", "failed", "clean", False, True,  True,  "prod",    "SA-DEPLOY", "BLOCK"),
    ("SVC-PAYMENTS-API", "deploy",  "D-FRONTEND", "passed", "clean", False, True,  True,  "prod",    "SA-DEPLOY", "BLOCK"),
    ("SVC-AUTH",         "test",    "D-IDENTITY", "passed", "clean", False, True,  True,  "staging", "SA-TEST",   "DISPATCH"),
    ("SVC-CHECKOUT-WEB", "monitor", "D-FRONTEND", "passed", "clean", False, True,  True,  "prod",    "SA-SRE",    "DISPATCH"),
    ("SVC-FEATURE-STORE","optimize","D-DATA",     "passed", "clean", False, True,  True,  "prod",    "SA-COST",   "DISPATCH"),
    ("SVC-AUTH",         "deploy",  "D-IDENTITY", "passed", "clean", True,  False, True,  "prod",    "SA-DEPLOY", "BLOCK"),
    # POL-SLO: solo aplica en prod, asi que el mismo hecho bloquea o no segun
    # el entorno. Es el gate el que depende del env, no el codigo.
    ("SVC-PAYMENTS-API", "deploy",  "D-PAYMENTS", "passed", "clean", False, True,  False, "prod",    "SA-DEPLOY", "BLOCK"),
    ("SVC-PAYMENTS-API", "deploy",  "D-PAYMENTS", "passed", "clean", False, True,  False, "staging", "SA-DEPLOY", "DISPATCH"),
]


def t_ab_ground_truth():
    orch = GovernedOrchestrator(with_slo(fresh()))
    for i, (svc, intent, dom, ci, sec, frz, apr, slo, env, route, verdict) in enumerate(GROUND_TRUTH, 1):
        r = orch.handle(dict(service=svc, intent=intent, requester_domain=dom,
                             ci_status=ci, security=sec, in_freeze_window=frz,
                             approved=apr, slo_ok=slo, env=env), record=False)
        assert r["handler_subagent"] == route, f"caso {i}: ruteo {r['handler_subagent']} != {route}"
        assert r["verdict"] == verdict, f"caso {i}: {r['verdict']} != {verdict} ({r['reason']})"


def t_decisiones_encadenadas():
    g = with_slo(fresh())
    orch = GovernedOrchestrator(g)
    r = orch.handle(dict(service="SVC-PAYMENTS-API", intent="deploy",
                         requester_domain="D-PAYMENTS", ci_status="passed",
                         security="clean", in_freeze_window=False,
                         approved=True, slo_ok=True, env="prod"))
    # 1 triage + 6 gates + 1 ruteo
    assert len(r["decisions"]) == 8, len(r["decisions"])
    chains = g.trace_decision_chain(r["decisions"][-1], max_steps=8)
    assert chains, "no se pudo reconstruir la cadena causal"
    best = max(chains, key=lambda c: c.get("hop_count", 0))
    assert best["hop_count"] >= 6, best["hop_count"]


def t_seed_no_trae_decisiones():
    """El grafo base es modelo + catalogo. Las decisiones son runtime."""
    g = fresh()
    assert [n for n in g.to_dict()["nodes"] if n["type"] == "decision"] == []


def t_catalogo_detecta_drift():
    """Un servicio sin dueño en CODEOWNERS no pasa en silencio."""
    import tempfile, os, shutil
    src = tempfile.mkdtemp()
    shutil.copytree(catalog.SOURCE_DIR, src, dirs_exist_ok=True)
    with open(os.path.join(src, "services", "huerfano.yaml"), "w") as f:
        f.write("apiVersion: backstage.io/v1alpha1\nkind: Component\n"
                "metadata:\n  name: huerfano\n  description: sin dueño\n"
                "  annotations:\n    idp/entity-id: SVC-HUERFANO\n"
                "spec:\n  type: service\n  lifecycle: C-BUILD\n  path: /services/huerfano/\n")
    g = ContextGraph(advanced_analytics=True)
    ontology.install(g)
    try:
        catalog.sync(g, source_dir=src, strict=True)
        raise AssertionError("deberia haber detectado el drift")
    except catalog.CatalogDriftError as exc:
        assert "huerfano" in str(exc)
    finally:
        shutil.rmtree(src, ignore_errors=True)


# ── 8: las señales son un HECHO servido, no una suposicion ────────────────
def t_señales_se_sirven_no_se_inventan():
    """El fichero de señales gobierna el veredicto sin tocar la ontologia."""
    import json, tempfile, os
    base = json.load(open(signals.SOURCE, encoding="utf-8"))
    orch = GovernedOrchestrator(fresh())
    req = dict(service="SVC-PAYMENTS-API", intent="deploy", env="prod")

    def veredicto(src_path):
        s = signals.resolve("SVC-PAYMENTS-API", "prod", path=src_path)
        return orch.handle({**req, **s}, record=False)

    fd, tmp = tempfile.mkstemp(suffix=".json"); os.close(fd)
    try:
        base["overrides"] = {}
        json.dump(base, open(tmp, "w", encoding="utf-8"))
        assert veredicto(tmp)["verdict"] == "DISPATCH"

        base["overrides"] = {"SVC-PAYMENTS-API@prod":
                             {"in_freeze_window": True, "approved": False}}
        json.dump(base, open(tmp, "w", encoding="utf-8"))
        r = veredicto(tmp)
        assert r["verdict"] == "BLOCK", r
        assert r["failed_gates"] == ["POL-FREEZE", "POL-APPROVAL"], r["failed_gates"]
    finally:
        os.unlink(tmp)


def t_origen_de_cada_señal_es_auditable():
    """No basta con saber que bloqueo: hay que decir de donde salio el hecho."""
    e = signals.explain("SVC-PAYMENTS-API", "prod")
    assert e["origin"]["requester_domain"] == "services.SVC-PAYMENTS-API"
    assert e["origin"]["in_freeze_window"] == "environments.prod"
    assert e["origin"]["ci_status"] == "defaults"
    assert "requester_domain" in e["non_spoofable"]


# ── 9: el grafo es CONSULTABLE desde el agente ────────────────────────────
def _shim():
    import semantica_mcp_shim as sh
    return sh


def t_resuelve_nombre_a_id():
    """El agente no debe pedirle al usuario identificadores internos."""
    sh = _shim()
    r = sh._tool_find_entity({"query": "payments-api", "type": "Service"})
    assert r["confidence"] == "high", r
    assert r["candidates"][0]["id"] == "SVC-PAYMENTS-API"

    r = sh._tool_find_entity({"query": "el servicio de pagos"})
    assert r["confidence"] == "high", r
    assert r["candidates"][0]["id"] == "SVC-PAYMENTS-API", r["candidates"][:2]


def t_no_finge_certeza():
    """Una coincidencia debil presentada como certeza es peor que ninguna."""
    sh = _shim()
    # Una pregunta de catalogo no es el nombre de una entidad: mejor devolver
    # vacio y redirigir a list_entities que colar el candidato menos malo.
    for pregunta in ("quien maneja los despliegues", "zzz no existe nada asi"):
        r = sh._tool_find_entity({"query": pregunta})
        assert r["confidence"] in ("none", "low"), (pregunta, r)
        assert r["candidates"] == [] or r["candidates"][0]["score"] < 0.40, r
        assert "list_entities" in r["instruction"] or "NO uses" in r["instruction"], r

    # ambiguo de verdad: 'pagos' es a la vez el dominio y el servicio
    r = sh._tool_find_entity({"query": "pagos"})
    assert r["confidence"] == "ambiguous", r


def t_describe_da_ambas_direcciones():
    """El dueño de un servicio esta en las ENTRANTES, no en las salientes."""
    sh = _shim()
    d = sh._tool_describe_entity({"id": "SVC-PAYMENTS-API"})
    incoming = {(e["edge"], e["from"]) for e in d["incoming"]}
    assert ("OWNS", "D-PAYMENTS") in incoming, d["incoming"]

    d = sh._tool_describe_entity({"id": "C-DEPLOY"})
    policies = {e["from"] for e in d["incoming"] if e["from_type"] == "Policy"}
    # el shim lee semantica_graph.json (el de la demo): se compara contra lo que
    # declara la ontologia, con o sin POL-SLO, en vez de inyectarla y persistirla
    expected = {p["id"] for p in ontology.POLICIES if "C-DEPLOY" in p["applies_to"]}
    assert policies == expected, (policies, expected)
    handlers = {e["from"] for e in d["incoming"] if e["from_type"] == "SubAgent"}
    assert handlers == {"SA-DEPLOY"}, handlers
    # las decisiones se cuentan, no se vuelcan
    assert all(e["from_type"] != "decision" for e in d["incoming"])


def t_id_inexistente_no_inventa():
    sh = _shim()
    d = sh._tool_describe_entity({"id": "SVC-NO-EXISTE"})
    assert "error" in d and "find_entity" in d["hint"]


def t_lista_el_catalogo():
    sh = _shim()
    d = sh._tool_list_entities({"type": "Service"})
    ids = {e["id"] for e in d["entities"]["Service"]}
    assert ids == {"SVC-PAYMENTS-API", "SVC-AUTH",
                   "SVC-FEATURE-STORE", "SVC-CHECKOUT-WEB"}, ids
    # y el catalogo no expone rastro de ejecucion
    todo = sh._tool_list_entities({})
    assert "decision" not in todo["entities"]


def t_politica_con_señal_nueva_sin_tocar_codigo():
    """Politica nueva + señal nueva = cero lineas de Python.

    Antes `runtime.py` armaba las señales con un dict fijo, asi que una politica
    que leyera algo nuevo nunca recibia el valor: habia que editar el codigo. Eso
    es el mismo error que hardcodear los gates.
    """
    g = fresh()
    g.add_node("POL-SLO", "Policy", content="No desplegar con el error budget agotado",
               name="SLO Budget Gate", gate_order=25, env_scope=["prod"],
               expression="slo_ok", layer="ontology")
    g.add_edge("POL-SLO", "C-DEPLOY", edge_type="APPLIES_TO")
    orch = GovernedOrchestrator(g)
    base = dict(service="SVC-PAYMENTS-API", intent="deploy", env="prod",
                requester_domain="D-PAYMENTS", ci_status="passed", security="clean",
                in_freeze_window=False, approved=True)

    # sin la señal: se niega a decidir, no asume que pasa
    try:
        orch.handle(base, record=False)
        raise AssertionError("deberia negarse sin la señal slo_ok")
    except PolicyExpressionError as exc:
        assert "slo_ok" in str(exc)

    # con la señal, y sin tocar runtime.py
    assert orch.handle({**base, "slo_ok": True}, record=False)["verdict"] == "DISPATCH"
    r = orch.handle({**base, "slo_ok": False}, record=False)
    assert r["verdict"] == "BLOCK" and r["failed_gates"] == ["POL-SLO"], r


def t_owner_domain_no_se_puede_pisar():
    """Aunque la peticion traiga owner_domain, gana el del grafo."""
    orch = GovernedOrchestrator(fresh())
    r = orch.handle(dict(service="SVC-PAYMENTS-API", intent="deploy", env="prod",
                         requester_domain="D-FRONTEND", owner_domain="D-FRONTEND",
                         ci_status="passed", security="clean",
                         in_freeze_window=False, approved=True,
                         slo_ok=True), record=False)
    assert r["owner_domain"] == "D-PAYMENTS", r["owner_domain"]
    assert r["verdict"] == "BLOCK" and r["failed_gates"] == ["POL-OWNERSHIP"], r


# ── 8: una herramienta alucinada no mata el turno ─────────────────────────
def t_herramienta_alucinada_no_revienta():
    """`describe_flaky_tool` no existe: debe volver como dato, no como crash.

    ADK 2.7.1 lanza ValueError desde `_get_tool` y, sin este callback, el nodo
    entero muere con traceback. El callback lo convierte en function_response.
    """
    from adk_orchestrator import unknown_tool_is_not_fatal

    error = ValueError(
        "Tool 'describe_flaky_tool' not found.\nAvailable tools: "
        "transfer_to_agent, describe_entity, find_entity\n\nPossible causes:\n"
        "  1. LLM hallucinated the function name"
    )
    r = unknown_tool_is_not_fatal(tool=None, args={}, tool_context=None, error=error)
    assert r is not None, "el turno moriria con traceback"
    assert r["error"] == "unknown_tool", r
    assert r["requested_tool"] == "describe_flaky_tool", r
    assert r["available_tools"] == [
        "transfer_to_agent", "describe_entity", "find_entity"], r


def t_error_real_de_herramienta_si_propaga():
    """Un fallo real del MCP NO se atrapa: debe seguir rompiendo.

    Si se tragara, un gate que no se pudo evaluar acabaria como texto que el
    modelo puede leer como que paso. Un gate no evaluado no es un gate.
    """
    from adk_orchestrator import unknown_tool_is_not_fatal

    for error in (RuntimeError("MCP session closed"),
                  ValueError("graph file is corrupt"),
                  ValueError("Tool 'x' not found."),  # sin lista: no es el caso
                  TimeoutError("timed out waiting for the session")):
        assert unknown_tool_is_not_fatal(
            tool=None, args={}, tool_context=None, error=error) is None, error


def t_todos_los_agentes_tienen_el_callback():
    """El gancho no sirve si falta en el agente que alucina."""
    import adk_orchestrator as ao

    agents = [ao.platform_orchestrator, ao.scaffold_agent, ao.test_agent,
              ao.deploy_agent, ao.sre_agent, ao.finops_agent]
    for a in agents:
        assert a.on_tool_error_callback is ao.unknown_tool_is_not_fatal, a.name


def t_el_subagente_no_retiene_el_turno():
    """La 2a peticion de la sesion debe volver al orquestador, no al sub-agente.

    Sin `disallow_transfer_to_parent`, ADK devuelve el turno a quien hablo
    ultimo. "Despliega payments-api a prod" caeria en `deploy_agent`, que no
    resuelve gobernanza ni lee señales: contesta de memoria. Los gates no
    fallarian, se saltarian.
    """
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService, Session
    from google.adk.events.event import Event
    import adk_orchestrator as ao

    root = ao.platform_orchestrator
    runner = Runner(app_name="t", agent=root,
                    session_service=InMemorySessionService())
    # sesion donde el ultimo en hablar fue el sub-agente de despliegue
    sess = Session(id="s", app_name="t", user_id="u", events=[
        Event(author="user", invocation_id="i1"),
        Event(author="platform_orchestrator", invocation_id="i1"),
        Event(author="deploy_agent", invocation_id="i1"),
    ])
    quien = runner._find_agent_to_run(sess, root)
    assert quien.name == "platform_orchestrator", (
        f"el turno se quedo en '{quien.name}': se salta los gates")


def t_todos_los_subagentes_son_terminales():
    import adk_orchestrator as ao
    for a in ao.platform_orchestrator.sub_agents:
        assert a.disallow_transfer_to_parent is True, a.name


TESTS = [
    ("T-Box rechaza tipo de arista desconocido",     t_edge_type_desconocido),
    ("T-Box rechaza dominio/rango invertido",        t_dominio_rango),
    ("grafo limpio pasa la validacion",              t_grafo_limpio_valida),
    ("una arista por hecho, dos direcciones",        t_una_arista_dos_direcciones),
    ("gates dependen del entorno (prod 6 / stg 3)",  t_gates_por_entorno),
    ("gates vienen ordenados por gate_order",        t_gates_ordenados),
    ("politica nueva sin tocar codigo",              t_politica_nueva_sin_tocar_codigo),
    ("intent -> capacidad sale del grafo",           t_intent_desde_el_grafo),
    ("evaluador de politicas: casos basicos",        t_evaluador_basico),
    ("evaluador rechaza codigo arbitrario",          t_evaluador_rechaza_codigo),
    ("evaluador exige las señales que lee",          t_evaluador_exige_señales),
    ("ownership se evalua contra el grafo",          t_ownership_usa_el_grafo),
    ("verdad-terreno A/B completa (9 casos)",        t_ab_ground_truth),
    ("decisiones encadenadas causalmente",           t_decisiones_encadenadas),
    ("el seed no trae decisiones",                   t_seed_no_trae_decisiones),
    ("el sync detecta drift de catalogo",            t_catalogo_detecta_drift),
    ("las señales se sirven, no se inventan",        t_señales_se_sirven_no_se_inventan),
    ("el origen de cada señal es auditable",         t_origen_de_cada_señal_es_auditable),
    ("resuelve nombre libre -> ID del grafo",        t_resuelve_nombre_a_id),
    ("no finge certeza en busquedas debiles",        t_no_finge_certeza),
    ("describe_entity da ambas direcciones",         t_describe_da_ambas_direcciones),
    ("un ID inexistente no se inventa",              t_id_inexistente_no_inventa),
    ("list_entities lista el catalogo",              t_lista_el_catalogo),
    ("politica con señal nueva, cero codigo",        t_politica_con_señal_nueva_sin_tocar_codigo),
    ("owner_domain del grafo no se puede pisar",     t_owner_domain_no_se_puede_pisar),
    ("herramienta alucinada no mata el turno",       t_herramienta_alucinada_no_revienta),
    ("error real de herramienta si propaga",         t_error_real_de_herramienta_si_propaga),
    ("todos los agentes llevan el callback",         t_todos_los_agentes_tienen_el_callback),
    ("el sub-agente no retiene el turno",            t_el_subagente_no_retiene_el_turno),
    ("los sub-agentes son terminales",               t_todos_los_subagentes_son_terminales),
]


if __name__ == "__main__":
    print("=" * 70)
    print("PRUEBAS DEL REDISEÑO EN TRES CAPAS")
    print("=" * 70)
    for name, fn in TESTS:
        check(name, fn)
    print("-" * 70)
    print(f"{len(PASS)} ok, {len(FAIL)} fallidas de {len(TESTS)}")
    if FAIL:
        for name, exc in FAIL:
            print(f"\n--- {name} ---")
            traceback.print_exception(type(exc), exc, exc.__traceback__)
    sys.exit(1 if FAIL else 0)
