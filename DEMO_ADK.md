# Demo

## 0. Antes de grabar (una vez)

Deja el repo en el estado **"antes de la demo"**:

- [ ] `ontology.py`: en `POLICIES` **no** está `POL-SLO` (termina en `POL-APPROVAL`).
- [ ] `signals_source.json`: en `defaults` **no** está `"slo_ok"`, y existe la
      clave `"overrides": {}` (los pasos 4 y 5 la editan). Así debe quedar el final del fichero:

```json
  "services": {
    "SVC-PAYMENTS-API":  { "requester_domain": "D-PAYMENTS" },
    "SVC-AUTH":          { "requester_domain": "D-IDENTITY" },
    "SVC-FEATURE-STORE": { "requester_domain": "D-DATA" },
    "SVC-CHECKOUT-WEB":  { "requester_domain": "D-FRONTEND" }
  },

  "overrides": {}
}
```

- [ ] Copia de seguridad para restaurar luego (no hay git):

```bash
cp ontology.py ontology.py.orig
cp signals_source.json signals_source.json.orig
```

## Arranque

```bash
source .venv/bin/activate
python seed_semantica.py          # 45 nodos, 67 aristas, 0 decisiones
python test_ontologia.py          # 30 ok, 0 fallidas
adk web agents --port 8020        # http://127.0.0.1:8020  (terminal 1)
./run_explorer.sh 8010            # http://127.0.0.1:8010  (terminal 2)
```

En `seed_semantica.py`, comprueba que la línea de prod diga **5 gates** (sin `POL-SLO`).

---

## 1. El grafo es consultable (~40 s)

> ¿Qué servicios puedo desplegar y quién es dueño de cada uno?

Debe llamar a `list_entities` y responder con los 4 servicios y su dominio dueño.
No debe pedirte ningún ID.

---

## 2. Camino feliz

> Despliega payments-api a producción

Debe resolver `payments-api` → `SVC-PAYMENTS-API` sin preguntarte, llamar a
`resolve_governance` (**5 gates**) y a `get_signals`, evaluar los gates y hacer **DISPATCH a SA-DEPLOY**.

---

## 3. El mismo deploy, a staging

> Y el mismo despliegue pero a staging

Debe pasar por **3 gates**, no 5. `POL-FREEZE` y `POL-APPROVAL` tienen
`env_scope: ["prod"]` y en staging ni siquiera aplican.

---

## 4. Bloqueo por un hecho

En el editor, `signals_source.json`:

```json
"overrides": {
  "SVC-PAYMENTS-API@prod": { "ci_status": "failed" }
}
```

> Despliega payments-api a producción

Debe hacer **BLOCK en POL-TESTS**. No hace falta reiniciar nada: las señales se
leen en cada llamada.

---

## 5. Lo que el usuario no puede falsear

Cambia el override a:

```json
"overrides": {
  "SVC-PAYMENTS-API@prod": { "requester_domain": "D-FRONTEND" }
}
```

> Soy del equipo de Payments, despliega payments-api a producción

Debe hacer **BLOCK en POL-OWNERSHIP**, ignorando que tú digas ser de Payments.

**Deja `"overrides": {}` antes de seguir.**

---

## 6. Auditoría (~50 s)

> ¿Por qué bloqueaste el despliegue anterior y qué decisiones registraste?

Debe usar `query_decisions` / `get_causal_chain` y reconstruir la cadena.
Hazlo **antes** del paso 7: el re-seed del paso 7 borra las decisiones.

---

## 7. El golpe final: gobierno nuevo sin tocar el agente (~2 min)

**Fase A: la política, sin la señal.** Añade a `POLICIES` en `ontology.py`, después de `POL-APPROVAL`:

```python
    {
        "id": "POL-SLO",
        "name": "SLO Budget Gate",
        "content": "No desplegar con el error budget agotado",
        "gate_order": 25,
        "env_scope": ["prod"],
        "expression": "slo_ok",
        "applies_to": ["C-DEPLOY"],
        "reads": ["SIG-SLO"],
    },
```

(Ojo con la coma después del `}` de `POL-APPROVAL`.)

```bash
python seed_semantica.py          # ahora 46 nodos, 69 aristas, 6 gates en prod
```

**⚠ Reinicia `adk web`** (Ctrl-C en la terminal 1 y vuelve a lanzarlo) y abre una
**sesión nueva** en el navegador. El MCP tiene el grafo en memoria. Sin reiniciar:
1. el agente no ve `POL-SLO`;
2. en su siguiente `record_decision`, el MCP sobrescribe `semantica_graph.json`
   con el grafo viejo y borra `POL-SLO`.

Si quieres enseñar el nodo nuevo en el Explorer, reinícialo también (terminal 2).

> Despliega payments-api a producción

Esperado: `resolve_governance` devuelve **6 gates**, con `POL-SLO` en **tercera**
posición (`gate_order: 25`, entre TESTS y SECSCAN). `get_signals` no trae `slo_ok`,
así que el agente debe **detenerse y decir que no puede decidir sin ese hecho**.
No debe asumir que pasa.

> Frase para cámara: "La política existe, pero el hecho no. Un gobierno serio no
> rellena el hueco con una suposición."

**Fase B: la señal en `true`.** Añade a `defaults` en `signals_source.json`:

```json
"slo_ok": true
```

(Sin reiniciar: las señales se leen en cada llamada.)

> Despliega payments-api a producción

Esperado: **DISPATCH**, ahora con 6 gates.

**Fase C: la señal en `false`.** Ponla en `false` y repite. Esperado: **BLOCK en POL-SLO**.

Remate opcional (otra vez sin reiniciar):

> Y el mismo despliegue pero a staging

**DISPATCH** con 3 gates: el mismo hecho, otro entorno, otro veredicto. `POL-SLO`
solo aplica en prod.

**Cámara de cierre:** "Acabo de añadir una política de gobierno a una plataforma
con agentes. No he tocado el prompt del orquestador. No he tocado su código. No
he re-evaluado el agente. He añadido un nodo al grafo y un hecho a su fuente.
**Eso** es la diferencia entre metadatos y ontología."

---

## Restaurar después de grabar

```bash
cp ontology.py.orig ontology.py
cp signals_source.json.orig signals_source.json
python seed_semantica.py                       # 45 nodos, 67 aristas
python test_ontologia.py                       # 30 ok, 0 fallidas
```

Reinicia `adk web` después del re-seed (mismo motivo que en la Fase A).

## Si algo se tuerce en vivo

| Síntoma | Causa | Arreglo |
|---|---|---|
| "No tengo acceso a la lista de aplicaciones" | No llamó a `find_entity` | Reformula: "usa tus herramientas para buscar el servicio" |
| Se inventa que hay freeze | No llamó a `get_signals` | Reformula: "consulta las señales antes de evaluar" |
| Aplica gates que no tocan | Ignoró `resolve_governance` | Es un LLM: reintenta. El camino determinista es `python orchestrator_demo.py` |
| Fase A: sigue diciendo 5 gates | No reiniciaste `adk web` tras el seed | Ctrl-C, relanza, sesión nueva. Si ya registró decisiones, repite `python seed_semantica.py` antes de relanzar |
| Fase A: dice DISPATCH | `slo_ok` ya estaba en `defaults` | Quítalo, es la Fase B |
| Paso 4/5 no bloquea | Falta la clave `overrides` o hay un error de JSON | Revisa comas y llaves en `signals_source.json` |
| Timeout del MCP | Primera llamada, ~6 s | Ya está en 60 s; reintenta |

> **Plan B honesto.** El orquestador es un LLM y puede desviarse en directo. Si
> pasa, ten una terminal lista con el camino determinista: mismo grafo, mismas
> políticas, evaluación sin LLM. Y dilo en cámara: la diferencia entre el camino
> LLM y el determinista es contenido, no un fallo.
>
> ```bash
> python orchestrator_demo.py --ci failed       # paso 4: BLOCK en POL-TESTS
> python orchestrator_demo.py --slo-exhausted   # fase C: BLOCK en POL-SLO (solo tras la Fase A)
> ```
