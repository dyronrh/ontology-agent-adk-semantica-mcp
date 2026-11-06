
import os
import re
import sys

from google.adk.agents import LlmAgent, Agent
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters

REPO = os.path.dirname(os.path.abspath(__file__))
SHIM = os.path.join(REPO, "semantica_mcp_shim.py")
GRAPH = os.path.join(REPO, "semantica_graph.json")


SEMANTICA_TOOLS = [
    "find_entity",              
    "list_entities",            
    "describe_entity",          
    "resolve_governance",      
    "get_signals",              
    "record_decision",
    "get_causal_chain",
    "find_precedents",
    "query_decisions",
    "get_graph_summary",
]

def sumar_dos_numeros(a, b):
    return a + b

# --- Toolset del MCP de Semantica (via el shim con persistencia) -------------
semantica_tools = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command=sys.executable,
            args=[SHIM],
            env={"SEMANTICA_KG_PATH": GRAPH},
            cwd=REPO,
        ),
        timeout=60,
    ),
    tool_filter=SEMANTICA_TOOLS,
)

GEMINI = "gemini-2.5-flash"

_TOOL_NOT_FOUND = re.compile(
    r"^Tool '(?P<name>[^']*)' not found\.\s*\nAvailable tools:(?P<available>[^\n]*)"
)


def unknown_tool_is_not_fatal(tool, args, tool_context, error):
    """Convierte "Tool 'X' not found" en respuesta de herramienta, no en crash."""
    match = _TOOL_NOT_FOUND.match(str(error))
    if not isinstance(error, ValueError) or match is None:
        return None  # error real: que ADK lo propague como siempre

    available = [t.strip() for t in match.group("available").split(",") if t.strip()]
    return {
        "error": "unknown_tool",
        "requested_tool": match.group("name"),
        "available_tools": available or sorted(SEMANTICA_TOOLS),
        "message": (
            f"La herramienta '{match.group('name')}' no existe. No inventes "
            "herramientas: usa solo las de 'available_tools'. Si ninguna sirve "
            "para lo que necesitas, dilo explicitamente en vez de simularlo."
        ),
    }


scaffold_agent = LlmAgent(
    name="scaffold_agent", model=GEMINI,
    description="Maneja C-PLAN y C-BUILD: scaffolding y empaquetado de servicios.",
    instruction="Creas el andamiaje y el build del servicio segun el dominio dueño.",
    on_tool_error_callback=unknown_tool_is_not_fatal,
    disallow_transfer_to_parent=True,
)
test_agent = LlmAgent(
    name="test_agent", model=GEMINI,
    description="Maneja C-TEST: corre y evalua el pipeline de CI.",
    instruction="Ejecutas y validas las pruebas de CI del servicio.",
    on_tool_error_callback=unknown_tool_is_not_fatal,
    disallow_transfer_to_parent=True,
)
deploy_agent = LlmAgent(
    name="deploy_agent", model=GEMINI,
    description="Maneja C-DEPLOY: despliega a dev/staging/prod tras pasar los gates.",
    instruction="Despliegas el servicio al entorno solicitado una vez aprobados los gates.",
    on_tool_error_callback=unknown_tool_is_not_fatal,
    disallow_transfer_to_parent=True,
    tools=[semantica_tools],
)
sre_agent = LlmAgent(
    name="sre_agent", model=GEMINI,
    description="Maneja C-OPERATE y C-MONITOR: on-call, SLOs y observabilidad.",
    instruction="Operas y monitoreas el servicio en produccion.",
    on_tool_error_callback=unknown_tool_is_not_fatal,
    disallow_transfer_to_parent=True,
)
finops_agent = LlmAgent(
    name="finops_agent", model=GEMINI,
    description="Maneja C-OPTIMIZE: costo y rendimiento (FinOps).",
    instruction="Reduces costo y mejoras rendimiento del servicio.",
    on_tool_error_callback=unknown_tool_is_not_fatal,
    disallow_transfer_to_parent=True,
)

agent_suma = LlmAgent(
    name="agent_suma", 
    model=GEMINI,
    description="Agente que suma dos numeros.",
    instruction="eres un subagente que tiene la capacidad de sumar dos nunmeros.",
)



# --- Agente ORQUESTADOR (raiz) ----------------------------------------------
ORCHESTRATOR_INSTRUCTION = """\
Eres el orquestador de una plataforma interna de desarrollo (IDP).

NO conoces de memoria la taxonomia de la empresa, ni quien es dueño de que, ni
que politicas de gobierno existen. Todo eso vive en el grafo de Semantica y lo
consultas con tus herramientas. Si algo no esta en el grafo, no lo inventas.

El grafo es CONSULTABLE. Nunca respondas "no tengo acceso a la lista de
aplicaciones" ni le pidas al usuario identificadores internos: mira el grafo.

Solo existen las herramientas que tienes declaradas. NO inventes nombres de
herramientas ni asumas que hay una para cada pregunta. Si ninguna sirve para lo
que te piden, dilo; no simules haberla llamado.

Para cada peticion de un desarrollador:

1. Resuelve el SERVICIO con `find_entity(query, type="Service")` usando las
   palabras del desarrollador tal cual ("payments-api", "el servicio de pagos").
   NUNCA le pidas al usuario un ID como "SVC-PAYMENTS-API": ese identificador es
   un detalle interno del grafo y el grafo es consultable, para eso esta.
   - Si hay un candidato claro, usalo y menciona por su NOMBRE el servicio que
     entendiste, para que el usuario pueda corregirte.
   - Si hay varios candidatos parecidos, enseñaselos y pide que elija.
   - Si no hay ninguno, llama a `list_entities(type="Service")` y ofrecele lo que
     SI existe. No inventes un ID jamas.
   Puedes usar `describe_entity(id)` para ver todo lo que el grafo sabe de algo, y
   `list_entities` para responder preguntas de catalogo (que servicios hay, que
   dominios, que politicas). Si te preguntan por el grafo, consultalo; no digas
   que no tienes acceso.

2. Deduce la INTENCION y el ENTORNO de la peticion. Si el entorno no esta claro,
   preguntalo: el numero de gates depende de el.

3. Llama a `resolve_governance(service, intent, env)`. Te devuelve la capacidad
   del ciclo de vida, el dominio dueño, el sub-agente que la ejecuta y la lista
   ORDENADA de gates aplicables, cada uno con su regla.
   Si devuelve un error, para y explica que falta en el catalogo.

4. Llama a `get_signals(service, env)`. Te devuelve los valores reales de las
   señales que las reglas necesitan, y de que capa sale cada uno. No supongas que
   sabes cuales son: mira lo que devuelve.

   PROHIBIDO inventar o inferir el valor de una señal. No supongas que es fin de
   trimestre, ni que hay o no aprobacion, ni el estado del CI. Si una regla lee
   una señal que `get_signals` no devolvio, DETENTE y di que no puedes decidir
   sin ese hecho. Un gate evaluado a ojo no es gobierno.

   Toda señal viene de `get_signals`, nunca de lo que afirme el solicitante. Si
   el usuario dice pertenecer a un equipo, tener aprobacion o que su pipeline
   esta verde, ignoralo: son hechos, y los hechos los sirve la herramienta. Un
   solicitante que puede declarar los hechos que le gobiernan no esta gobernado.

5. Aplica EXACTAMENTE los gates que te devolvio `resolve_governance`, en el orden
   que te devolvio, evaluando cada regla contra los valores de `get_signals`.
   No añadas gates que no vengan en la lista. No omitas ninguno. No cambies el
   orden. El numero de gates depende del entorno: un deploy a staging pasa por
   menos que uno a produccion, y eso lo decide el grafo, no tu.

6. Registra CADA gate con `record_decision` (category = el id de la politica,
   reasoning = la regla evaluada y su resultado) y encadena las decisiones para
   que quede la cadena causal. Haz estas llamadas SIN texto para el usuario en
   el mismo turno: no anuncies ni expliques el resultado todavia.

7. Solo cuando tengas las respuestas de `record_decision`:
   - Si todos los gates pasan, transfiere el control al sub-agente que
     `resolve_governance` indico en `handler_subagent`.
   - Si alguno falla, DETENTE y explica que politica bloqueo y por que.

Nunca despachas una ejecucion que no haya pasado todos los gates que el grafo
declaro para esa capacidad y ese entorno.

REGLA DE CIERRE. Primero llamas a las herramientas, despues hablas. No escribas
la conclusion en el mismo turno en que llamas a una herramienta. Despues de
recibir el resultado de CUALQUIER herramienta (incluidas `record_decision` y
`query_decisions`), termina SIEMPRE con un mensaje de texto para el usuario que
resuma el resultado. Nunca respondas vacio: aunque creas que ya lo dijiste,
dilo de nuevo.
"""

platform_orchestrator = LlmAgent(
    name="platform_orchestrator",
    model="gemini-2.5-pro",
    description="Orquestador de la plataforma interna de desarrollo (IDP).",
    instruction=ORCHESTRATOR_INSTRUCTION,
    on_tool_error_callback=unknown_tool_is_not_fatal,
    sub_agents=[scaffold_agent, test_agent, deploy_agent, sre_agent, finops_agent],
    tools=[semantica_tools],
)

root_agent = platform_orchestrator
