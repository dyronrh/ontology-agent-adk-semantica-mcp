"""
orchestrator_demo.py
--------------------
Una peticion, resuelta CONSULTANDO el grafo, con su rastro auditable.

    python seed_semantica.py
    python orchestrator_demo.py                  # camino feliz
    python orchestrator_demo.py --env staging    # menos gates, mismo codigo
    python orchestrator_demo.py --ci failed      # bloquea en el gate de CI
    python orchestrator_demo.py --requester D-FRONTEND   # bloquea en ownership
    python orchestrator_demo.py --slo-exhausted  # bloquea en POL-SLO (solo prod)
    python orchestrator_demo.py --save           # persiste las decisiones al grafo

Lo importante: en este fichero NO hay una lista de gates. Ni su orden. Ni sus
reglas. `GovernedOrchestrator` pregunta al grafo que politicas aplican a
(capacidad, entorno) y aplica lo que reciba. Cambia `ontology.py` y esta demo
cambia de comportamiento sin tocarse.
"""
import argparse

from semantica.context import ContextGraph

from runtime import (GovernedOrchestrator, PolicyExpressionError,
                     format_resolution)

GRAPH_PATH = "semantica_graph.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--service", default="SVC-PAYMENTS-API")
    ap.add_argument("--intent", default="deploy")
    ap.add_argument("--env", default="prod", choices=["dev", "staging", "prod"])
    ap.add_argument("--requester", default="D-PAYMENTS")
    ap.add_argument("--ci", default="passed", choices=["passed", "failed"])
    ap.add_argument("--security", default="clean", choices=["clean", "critical"])
    ap.add_argument("--freeze", action="store_true", help="ventana de freeze activa")
    ap.add_argument("--no-approval", action="store_true", help="sin aprobacion vigente")
    ap.add_argument("--slo-exhausted", action="store_true",
                    help="error budget del SLO agotado (POL-SLO, solo prod)")
    ap.add_argument("--save", action="store_true", help="persistir decisiones al grafo")
    args = ap.parse_args()

    g = ContextGraph(advanced_analytics=True)
    g.load_from_file(GRAPH_PATH)
    orch = GovernedOrchestrator(g)

    request = {
        "text": f"{args.intent} {args.service} a {args.env}",
        "service": args.service,
        "intent": args.intent,
        "env": args.env,
        "requester_domain": args.requester,
        "ci_status": args.ci,
        "security": args.security,
        "in_freeze_window": args.freeze,
        "approved": not args.no_approval,
        "slo_ok": not args.slo_exhausted,
    }

    print("=" * 74)
    print(f"PETICION: {request['text']!r}  (pide: {args.requester})")
    print("=" * 74)

    # 1) Lo que el GRAFO responde, antes de decidir nada
    ctx = orch.resolve(args.service, args.intent, args.env)
    print("\nRESOLUCION CONTRA LA ONTOLOGIA")
    print(format_resolution(ctx))

    # 2) Ejecucion gobernada
    try:
        result = orch.handle(request)
    except PolicyExpressionError as exc:
        # Esto pasa cuando añades una politica al grafo cuya señal no llega en la
        # peticion. No es un bug: una politica no se puede evaluar a ciegas, asi
        # que el orquestador se niega a decidir en vez de asumir que pasa.
        print("\n" + "!" * 74)
        print(f"GOBERNANZA INCOMPLETA: {exc}")
        print("El orquestador NO decide sin la señal. Declarala en SIGNALS de")
        print("ontology.py, sirvela en signals_source.json y pasala en la peticion.")
        print("!" * 74)
        raise SystemExit(2)

    print("\nEVALUACION DE GATES (orden y reglas: del grafo)")
    for r in result["gate_results"]:
        mark = "PASS " if r["passed"] else "BLOCK"
        print(f"  [{mark}] {r['order']:>3}. {r['policy']:<14} {r['expression']}")

    print("\n" + "-" * 74)
    print(f"VEREDICTO: {result['verdict']}   ({result['reason']})")
    if result["verdict"] == "DISPATCH":
        print(f"           -> despacha a {result['handler_subagent']}")
    print(f"Decisiones registradas: {len(result['decisions'])} "
          f"(encadenadas con CAUSED)")

    # 3) Auditoria: la cadena causal desde la primera decision
    print("-" * 74)
    # trace_decision_chain camina HACIA ATRAS (de una decision a sus causas),
    # asi que se parte de la ultima decision para reconstruir todo el camino.
    print("CADENA CAUSAL (desde la decision final, hacia sus causas):")
    chains = g.trace_decision_chain(result["decisions"][-1], max_steps=8)
    if chains:
        best = max(chains, key=lambda c: c.get("hop_count", 0))
        for hop in best["hops"]:
            print(f"   {hop['from_scenario'][:32]:<34} --{hop['type']}-->  "
                  f"{hop['to_scenario'][:32]}")
        print(f"   (confianza a lo largo de la cadena: {best['confidence_decay']:.2f})")

    if args.save:
        orch.save(GRAPH_PATH)
        print("-" * 74)
        print(f"Decisiones persistidas en {GRAPH_PATH}. Reinicia el explorer para verlas.")


if __name__ == "__main__":
    main()
