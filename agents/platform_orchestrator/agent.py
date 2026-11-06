"""
agents/platform_orchestrator/agent.py
-------------------------------------
Punto de entrada que `adk web` descubre.

`adk web` NO mira ficheros sueltos: escanea el directorio que le pasas y trata
cada SUBDIRECTORIO como un agente, siempre que contenga `agent.py`,
`__init__.py` o `root_agent.yaml`, y exponga una variable `root_agent`. Por eso
`adk_orchestrator.py` en la raiz del repo nunca aparecia en la lista: no es un
paquete, es un fichero.

La definicion real sigue viviendo en `adk_orchestrator.py` (es la que sale en el
video). Aqui solo se re-exporta, para que haya UNA sola definicion del agente.

    adk web agents          # desde la raiz del repo
"""
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from adk_orchestrator import root_agent  # noqa: E402

__all__ = ["root_agent"]
