"""
seed_semantica.py
-----------------
Compone el grafo del IDP y lo exporta a `semantica_graph.json`.

    python seed_semantica.py
    ./run_explorer.sh

Este fichero YA NO contiene datos. Solo orquesta las tres capas, que son tres
ciclos de vida distintos y por eso viven separadas:

    ontology.py   T-Box     el MODELO      humano, git, trimestral
    catalog.py    A-Box     los HECHOS     sync desde catalog_source/, diario
    runtime.py    -         DECISIONES     el agente, en runtime, por peticion

Lo que salio de aqui y por que:

  · Las 6 decisiones que este seed escribia a mano se borraron. Eran rastro de
    ejecucion inventado — el equivalente a commitear los logs. Ahora las produce
    `runtime.py` cuando el orquestador realmente decide. Si quieres poblarlas
    para grabar, corre `python orchestrator_demo.py --save`.

  · Los dominios, servicios y sub-agentes hardcodeados se mudaron a
    `catalog_source/` y se derivan por sync (CODEOWNERS incluido).

  · El grafo se valida contra el T-Box antes de exportarse: si una arista no
    respeta el vocabulario, esto falla aqui y no en produccion.
"""
import argparse
import json

from semantica.context import ContextGraph

import catalog
import ontology
from graph_query import GraphQuery

GRAPH_PATH = "semantica_graph.json"


def build(strict: bool = True) -> ContextGraph:
    g = ContextGraph(advanced_analytics=True)

    print("CAPA 1/3  ontologia (T-Box) — el modelo")
    onto = ontology.install(g)
    print(f"  capacidades={onto['capabilities']}  intenciones={onto['intents']}  "
          f"señales={onto['signals']}  politicas={onto['policies']}  aristas={onto['edges']}")

    print("CAPA 2/3  catalogo (A-Box) — sincronizado desde catalog_source/")
    cat = catalog.sync(g, strict=strict)
    print(f"  dominios={cat['domains']}  servicios={cat['services']}  "
          f"sub-agentes={cat['subagents']}  aristas={cat['edges']}")
    if cat["drift"]:
        for d in cat["drift"]:
            print(f"  DRIFT: {d}")

    print("CAPA 3/3  decisiones — vacio a proposito (las escribe el runtime)")

    problems = ontology.validate(g, strict=strict)
    print(f"validacion T-Box: {'OK' if not problems else str(len(problems)) + ' problema(s)'}")
    return g


def main() -> None:
    ap = argparse.ArgumentParser(description="Construye el grafo del IDP.")
    ap.add_argument("--out", default=GRAPH_PATH, help=f"destino (default: {GRAPH_PATH})")
    ap.add_argument("--lax", action="store_true",
                    help="no abortar ante drift de catalogo o violaciones del T-Box")
    args = ap.parse_args()

    g = build(strict=not args.lax)
    g.save_to_file(args.out)

    data = g.to_dict()
    print("-" * 70)
    print(f"OK -> {args.out}  ({len(data['nodes'])} nodos, {len(data['edges'])} aristas)")

    # Prueba viva de que el gobierno ya se puede CONSULTAR, no recitar.
    q = GraphQuery(g)
    for env in ("staging", "prod"):
        gates = q.gates_for("C-DEPLOY", env)
        print(f"gates para C-DEPLOY en {env:<8}: "
              f"{[gate['id'] for gate in gates]}")


if __name__ == "__main__":
    main()
