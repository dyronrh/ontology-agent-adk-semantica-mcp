#!/usr/bin/env bash
# Levanta el Semantica Knowledge Explorer sobre semantica_graph.json.
#
# Uso:
#   ./run_explorer.sh              # puerto 8010
#   ./run_explorer.sh 8020         # puerto a medida
#
# Nota de seguridad: el Explorer exige autenticacion en sus rutas /api/*.
# Si SEMANTICA_API_KEY no esta definida devuelve 503 en todas ellas y la UI
# se queda vacia. La UI estatica no envia la cabecera X-API-Key, asi que para
# uso local se opta explicitamente por acceso anonimo, y SOLO en loopback.
set -euo pipefail

cd "$(dirname "$0")"

PORT="${1:-8010}"
HOST="127.0.0.1"
GRAPH="semantica_graph.json"

if [ ! -f "$GRAPH" ]; then
  echo "No existe $GRAPH. Ejecuta primero: python seed_semantica.py" >&2
  exit 1
fi

PY="./.venv/bin/python"
[ -x "$PY" ] || PY="python"

export SEMANTICA_ALLOW_ANONYMOUS=true
# run_explorer.py = semantica-explorer + vigilante del fichero: las decisiones
# que registra el agente aparecen sin reiniciar el Explorer.
exec "$PY" run_explorer.py --graph "$GRAPH" --host "$HOST" --port "$PORT"
