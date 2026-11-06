"""
signals.py
----------
Sirve los VALORES de las señales que las politicas leen.

Por que hace falta. La ontologia declara con aristas `EVALUATES` que señales
necesita cada politica (POL-TESTS lee SIG-CI, POL-FREEZE lee SIG-FREEZE y
SIG-APPROVAL...), pero declarar que una señal existe no es lo mismo que servir
su valor. Sin esto, el agente LLM que aplica los gates **se inventa los hechos**:
decide por su cuenta que "es fin de trimestre y no hay aprobacion de emergencia"
y bloquea por algo que nadie le dijo.

Un gate que se evalua contra datos inventados no es gobierno, es teatro.

En este repo los valores viven en `signals_source.json`, que es el fichero que
tocas para que una demo pase o bloquee. En produccion cada `_read_*` de aqui
llama a su sistema real: la API de tu CI, el calendario de cambios, el sistema
de aprobaciones. La forma no cambia.
"""
from __future__ import annotations

import json
import os
from typing import Any

SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signals_source.json")

# Señales que el solicitante NO puede declarar por si mismo. Si `requester_domain`
# viniera del prompt, POL-OWNERSHIP seria decorativo: bastaria con decir "soy de
# Payments". En produccion sale del token de autenticacion.
NON_SPOOFABLE = {"requester_domain"}


class SignalsUnavailable(Exception):
    """No se pudo determinar el valor de una señal. Nunca se asume un default."""


def _load(path: str | None = None) -> dict[str, Any]:
    with open(path or SOURCE, encoding="utf-8") as f:
        return json.load(f)


def resolve(service: str, env: str, path: str | None = None) -> dict[str, Any]:
    """Valores efectivos de las señales para (servicio, entorno).

    Prioridad: defaults < environments[env] < services[svc] < overrides[svc@env]
    """
    src = _load(path)
    out: dict[str, Any] = {}
    out.update(src.get("defaults", {}))
    out.update(src.get("environments", {}).get(env, {}))
    out.update(src.get("services", {}).get(service, {}))
    out.update(src.get("overrides", {}).get(f"{service}@{env}", {}))
    return out


def explain(service: str, env: str, path: str | None = None) -> dict[str, Any]:
    """Igual que `resolve`, pero diciendo de que capa salio cada valor.

    Sirve para la auditoria: no basta con saber que el gate bloqueo, hay que
    poder decir de donde salio el hecho que lo bloqueo.
    """
    src = _load(path)
    layers = [
        ("defaults", src.get("defaults", {})),
        (f"environments.{env}", src.get("environments", {}).get(env, {})),
        (f"services.{service}", src.get("services", {}).get(service, {})),
        (f"overrides.{service}@{env}", src.get("overrides", {}).get(f"{service}@{env}", {})),
    ]
    values, origin = {}, {}
    for name, layer in layers:
        for k, v in layer.items():
            values[k] = v
            origin[k] = name
    return {"values": values, "origin": origin,
            "non_spoofable": sorted(NON_SPOOFABLE & set(values))}
