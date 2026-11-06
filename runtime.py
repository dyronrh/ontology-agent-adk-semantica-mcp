"""
runtime.py  —  CAPA 3 de 3: EJECUCION
--------------------------------------
El orquestador gobernado. Aqui NO se declara nada del modelo ni del catalogo:
todo se CONSULTA al grafo. Lo unico que este modulo produce son decisiones, que
son rastro de ejecucion (append-only, nunca escritas a mano).

Ciclo de vida:  escrito por el agente · por peticion · nunca versionado en git.

La diferencia de fondo con la version anterior: antes la lista y el ORDEN de los
gates estaban escritos dos veces — en el prompt del agente ADK
("ownership -> CI -> seguridad -> freeze/aprobacion") y en el codigo de la demo
(un dict `gates = {...}` a mano). Añadir una politica obligaba a tocar las dos.

Ahora `GovernedOrchestrator` pregunta `query.gates_for(capability, env)` y aplica
EXACTAMENTE lo que el grafo devuelva, en el orden que el grafo diga. Este fichero
no menciona ni una politica por su nombre.
"""
from __future__ import annotations

import ast
from typing import Any, Optional

from graph_query import GraphQuery


# ══════════════════════════════════════════════════════════════════════════
# Evaluador de expresiones de politica
#
# Las reglas viven en el grafo como texto ("ci_status == 'passed'"). Para que
# eso sea seguro NO se usa eval(): se camina el AST y solo se permite un
# subconjunto booleano. Cualquier llamada, atributo o import revienta.
# ══════════════════════════════════════════════════════════════════════════
class PolicyExpressionError(Exception):
    pass


_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
    ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.In, ast.NotIn, ast.Name, ast.Load, ast.Constant, ast.List, ast.Tuple,
)


def evaluate_policy(expression: str, context: dict[str, Any]) -> bool:
    """Evalua la expresion de una politica contra las señales de la peticion."""
    if not expression.strip():
        return True
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise PolicyExpressionError(f"expresion invalida {expression!r}: {exc}") from exc

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise PolicyExpressionError(
                f"construccion no permitida en {expression!r}: {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id not in context:
            raise PolicyExpressionError(
                f"la politica lee '{node.id}', que no viene en las señales de la peticion")

    return bool(eval(compile(tree, "<policy>", "eval"), {"__builtins__": {}}, dict(context)))


# ══════════════════════════════════════════════════════════════════════════
# El orquestador
# ══════════════════════════════════════════════════════════════════════════
class GovernedOrchestrator:
    """Resuelve taxonomia y gobierno CONSULTANDO el grafo, y deja rastro."""

    def __init__(self, graph, decision_maker: str = "AGENT-ORCH"):
        self.g = graph
        self.q = GraphQuery(graph)
        self.decision_maker = decision_maker

    # -- resolucion (todo sale del grafo) ---------------------------------
    def resolve(self, service_id: str, intent: str, env: str) -> dict:
        capability = self.q.capability_for_intent(intent)
        owner = self.q.owner_domain(service_id)
        handler = self.q.handler_subagent(capability) if capability else None
        gates = self.q.gates_for(capability, env) if capability else []
        return {
            "service": service_id,
            "intent": intent,
            "env": env,
            "capability": capability,
            "owner_domain": owner,
            "handler_subagent": handler,
            "gates": gates,
        }

    # -- ejecucion --------------------------------------------------------
    def handle(self, request: dict, record: bool = True) -> dict:
        """Procesa una peticion. Devuelve veredicto + rastro.

        `request` trae las señales: service, intent, env, requester_domain,
        ci_status, security, in_freeze_window, approved.
        """
        service = request["service"]
        env = request.get("env", "dev")
        ctx = self.resolve(service, request["intent"], env)

        if not ctx["capability"]:
            return {**ctx, "verdict": "REJECT", "reason":
                    f"intencion '{request['intent']}' no realiza ninguna capacidad conocida",
                    "gate_results": [], "decisions": []}

        # Contexto de evaluacion = señales de la peticion + hechos del grafo.
        #
        # Se pasan TODAS las claves de la peticion que no sean estructurales. Un
        # dict fijo aqui seria el mismo error que hardcodear los gates: al añadir
        # una politica que lee una señal nueva habria que editar este fichero, y
        # el objetivo es justo el contrario. Ahora: politica nueva en el grafo +
        # señal nueva en signals_source.json, cero lineas de codigo.
        STRUCTURAL = {"service", "intent", "env", "text"}
        signals = {k: v for k, v in request.items() if k not in STRUCTURAL}

        # `owner_domain` lo pone el GRAFO, siempre, y pisa cualquier valor que
        # venga en la peticion: es el hecho que el solicitante no puede falsear.
        signals["owner_domain"] = ctx["owner_domain"]
        signals["env"] = env
        for flag in ("in_freeze_window", "approved"):
            if flag in signals:
                signals[flag] = bool(signals[flag])

        decisions: list[str] = []
        gate_results: list[dict] = []

        if record:
            d_triage = self.g.record_decision(
                category="request_triage",
                scenario=request.get("text", f"{request['intent']} {service}"),
                reasoning=(f"{service} pertenece a {ctx['owner_domain']}; "
                           f"intent={request['intent']} REALIZES {ctx['capability']}; "
                           f"el grafo exige {len(ctx['gates'])} gate(s) en {env}"),
                outcome=f"domain={ctx['owner_domain']},capability={ctx['capability']}",
                confidence=0.96, decision_maker=self.decision_maker,
                entities=[e for e in ("SIG-REQUEST", service, ctx["capability"],
                                      ctx["owner_domain"]) if e],
                metadata={"capability": ctx["capability"], "env": env},
            )
            decisions.append(d_triage)

        # --- los gates: EXACTAMENTE los que el grafo diga, en su orden -----
        for gate in ctx["gates"]:
            passed = evaluate_policy(gate["expression"], signals)
            gate_results.append({
                "policy": gate["id"], "name": gate["name"], "order": gate["order"],
                "expression": gate["expression"], "passed": passed,
                "env_scope": gate["env_scope"],
            })
            if record:
                decisions.append(self.g.record_decision(
                    category=gate["id"].replace("POL-", "").lower() + "_gate",
                    scenario=f"{gate['name']} sobre {service} ({ctx['capability']} @ {env})",
                    reasoning=f"regla del grafo: {gate['expression']} -> {passed}",
                    outcome="pass" if passed else "block",
                    confidence=0.95, decision_maker=self.decision_maker,
                    entities=[gate["id"], service] + gate["reads"],
                    metadata={"capability": ctx["capability"], "env": env,
                              "policy": gate["id"], "gate_order": gate["order"]},
                ))

        failed = [r["policy"] for r in gate_results if not r["passed"]]
        verdict = "DISPATCH" if not failed else "BLOCK"

        if record:
            decisions.append(self.g.record_decision(
                category="orchestration_route",
                scenario=f"enrutar {ctx['capability']} de {service} en {env}",
                reasoning=(f"{ctx['handler_subagent']} HANDLES {ctx['capability']}; "
                           f"gates fallidos={failed or 'ninguno'}"),
                outcome=(f"dispatch_to_{ctx['handler_subagent']}" if verdict == "DISPATCH"
                         else f"halt_{'+'.join(failed)}"),
                confidence=0.95, decision_maker=self.decision_maker,
                entities=[e for e in (ctx["handler_subagent"], ctx["capability"], service) if e],
                metadata={"capability": ctx["capability"], "env": env, "verdict": verdict},
            ))
            for a, b in zip(decisions, decisions[1:]):
                self.g.add_causal_relationship(a, b, relationship_type="CAUSED")
            self.q.refresh()

        return {
            **ctx,
            "verdict": verdict,
            "gate_results": gate_results,
            "failed_gates": failed,
            "reason": (f"gate(s) fallido(s): {', '.join(failed)}" if failed
                       else f"los {len(gate_results)} gates del grafo pasaron"),
            "decisions": decisions,
        }

    # -- persistencia ------------------------------------------------------
    def save(self, path: str) -> None:
        self.g.save_to_file(path)


def format_resolution(ctx: dict) -> str:
    """Render legible de lo que el grafo respondio (para demo y para el video)."""
    lines = [
        f"  servicio        : {ctx['service']}",
        f"  intencion       : {ctx['intent']}  -[REALIZES]->  {ctx['capability']}",
        f"  dominio dueño   : {ctx['owner_domain']}   (via Domain-[OWNS]->Service)",
        f"  sub-agente      : {ctx['handler_subagent']}   (via SubAgent-[HANDLES]->Capability)",
        f"  entorno         : {ctx['env']}",
        f"  gates aplicables: {len(ctx['gates'])}  (via Policy-[APPLIES_TO]->Capability)",
    ]
    for gate in ctx["gates"]:
        scope = f" [solo {','.join(gate['env_scope'])}]" if gate["env_scope"] else ""
        lines.append(f"      {gate['order']:>3}. {gate['id']:<14} {gate['expression']}{scope}")
    return "\n".join(lines)
