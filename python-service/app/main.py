import uvicorn
from fastapi import FastAPI, Request
from pydantic import BaseModel
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import ToolNode
from langchain_core.messages import HumanMessage, AIMessage, BaseMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from typing import Optional, Dict, Any, Literal, List
import json
import asyncio

# 1. Importaciones de la nueva arquitectura
from .state import GlobalState
from .tools import (
    all_tools, knowledge_search, check_availability, 
    select_appointment_slot, book_appointment,
    update_service_in_state, 
    escalate_to_human, get_user_appointments, cancel_appointment
)
from langchain_core.runnables import RunnableConfig
from langchain_core.load import dumps, loads
import os
from pydantic import BaseModel as PydanticBaseModel
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from .db import supabase_client, run_db
from .memory import (
    get_last_messages as sb_get_last_messages,
    get_context_block as sb_get_context_block,
    upsert_thread_summary as sb_upsert_thread_summary,
)
from langchain_openai import ChatOpenAI
 
# Integración opcional con Langfuse (observabilidad LLM)
LANGFUSE_ENABLED = bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY") and os.getenv("LANGFUSE_HOST"))
lf_handler = None
if LANGFUSE_ENABLED:
    try:
        # Preferido en versiones recientes
        from langfuse.callback import CallbackHandler as LangfuseCallbackHandler
        lf_handler = LangfuseCallbackHandler()
        print("🛰️ Langfuse habilitado para trazas LLM")
    except Exception:
        try:
            # Compatibilidad con layout anterior
            from langfuse.callback.langchain import CallbackHandler as LangfuseCallbackHandler
            lf_handler = LangfuseCallbackHandler()
            print("🛰️ Langfuse habilitado para trazas LLM (compat)")
        except Exception as _e:
            lf_handler = None
            print(f"⚠️ Langfuse deshabilitado (no se pudo importar CallbackHandler): {_e}")

# --- 2. Supervisor y Enrutador ---
# Eliminado CHECKPOINT_NS; no se usa con MemorySaver

class Route(PydanticBaseModel):
    """Decide a qué nodo dirigir la conversación a continuación."""
    next: Literal[
        "knowledge", "appointment", "cancellation", "confirmation", "reschedule",
        "escalation", "__end__"
    ]

# Permite configurar el modelo por variable de entorno (p. ej., OPENAI_CHAT_MODEL=gpt-4.1-nano)
model_name = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o")
# No pasar temperature explícito: algunos modelos (nano) solo soportan el valor por defecto
llm = ChatOpenAI(model=model_name, callbacks=[lf_handler] if lf_handler else None)
print(f"⚙️ Modelo OpenAI activo: {model_name} (Temperatura: default)")
structured_llm_router = llm.with_structured_output(Route)

# Utilidad para extraer un mensaje final útil del grafo
def _extract_final_ai_content(messages: List[BaseMessage]) -> Optional[str]:
    """Devuelve el contenido del AIMessage más reciente SIN tool_calls.
    Si no hay, devuelve el contenido del AIMessage más reciente (aunque tenga tool_calls).
    Si no hay AIMessage, None.
    """
    last_ai_with_tools: Optional[AIMessage] = None
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            if not getattr(msg, "tool_calls", None):
                return msg.content
            last_ai_with_tools = msg
    return getattr(last_ai_with_tools, "content", None)


async def _maybe_update_thread_summary(organization_id: str, chat_identity_id: str, last_messages: List[Dict[str, Any]]):
    """Genera y guarda un resumen del hilo cuando hay suficiente contexto.
    Estrategia simple: si hay >= 8 mensajes recientes, generar resumen breve.
    """
    try:
        if len(last_messages) < 8:
            return
        # Construir texto base
        joined = "\n".join([f"{m.get('role')}: {m.get('content','')[:400]}" for m in last_messages][-20:])
        prompt = ChatPromptTemplate.from_messages([
            ("system", "Resume de forma breve y factual la conversación, destacando: intención, datos clave (fechas/horas/servicio), decisiones y próximos pasos. Máx 120-160 palabras."),
            ("user", joined),
        ])
        summary = (await (prompt | llm).ainvoke({})).content
        if summary:
            await sb_upsert_thread_summary(organization_id, chat_identity_id, summary)
    except Exception as e:
        print(f"⚠️ No se pudo actualizar el resumen del hilo: {e}")

supervisor_prompt = ChatPromptTemplate.from_messages(
    [
        ("system", """
Eres el supervisor de un sistema de agentes de IA para un centro de estética. Tu única tarea es enrutar la conversación al nodo correcto.

**Nodos Disponibles:**
- `knowledge`: Para saludos iniciales y preguntas generales sobre la empresa o servicios. Es puramente informativo.
- `appointment`: Cuando el usuario expresa explícitamente el deseo de agendar, reservar o pedir una cita.
- `cancellation`: Si el usuario quiere cancelar o reprogramar una cita existente.
- `confirmation`: Si el usuario quiere confirmar una cita ya programada.
- `reschedule`: Si el usuario quiere reagendar/reprogramar/cambiar fecha u hora de una cita ya creada.
- `escalation`: Si el usuario explícitamente confirma que quiere hablar con un asesor (ej. "sí, quiero hablar con un asesor", "sí, por favor", "necesito ayuda humana").
- `__end__`: Únicamente para despedidas explícitas (ej. "adiós", "chao") o cuando la conversación ha concluido.

**Contexto (Memoria persistente):**
{context_block}
**Últimos mensajes (Memoria a Corto Plazo):**
{messages}
Basado en el **último mensaje del usuario** y el contexto, decide el siguiente nodo."""),
        ("user", "{last_message}"),
    ]
)

async def supervisor_node(state: GlobalState) -> Dict[str, Any]:
    print("--- 🧠 NODO: Supervisor ---")
    
    if isinstance(state["messages"][-1], ToolMessage):
        print(f"🚦 Devolviendo control a '{state['current_flow']}' tras ejecución de herramienta.")
        # Debug: verificar si los slots están en el estado después de tools
        available_slots = state.get('available_slots')
        if available_slots is not None:
            print(f"🚦 Estado de slots en supervisor: {len(available_slots)} slots disponibles")
        else:
            print(f"🚦 Estado de slots en supervisor: No hay slots en el estado")
        return {"next_agent": state['current_flow']}

    last_message = state["messages"][-1].content
    chain = supervisor_prompt | structured_llm_router
    route = await chain.ainvoke({
        "context_block": state.get("context_block", "No hay resumen."),
        "messages": "\n".join([f"{type(m).__name__}: {m.content}" for m in state["messages"][-6:]]),
        "last_message": last_message,
    })
    # Heurística de persistencia de flujo: evita saltos accidentales al nodo de agendamiento
    preferred_next = route.next
    try:
        current_flow = state.get("current_flow")
        msg_lc = (last_message or "").lower()
        if current_flow in ("confirmation", "cancellation", "reschedule"):
            phrases_uncertain = [
                "no recuerdo", "no me acuerdo", "no sé la fecha", "no se la fecha",
                "no tengo la fecha", "no recuerdo la fecha", "no recuerdo la hora",
                "no sé la hora", "no se la hora"
            ]
            if any(p in msg_lc for p in phrases_uncertain):
                preferred_next = current_flow
        # No saltar a appointment desde confirmation salvo intención explícita de agendar
        if current_flow == "confirmation" and route.next == "appointment":
            intent_schedule = ["agendar", "reservar", "programar", "quiero agendar", "quiero reservar"]
            if not any(w in msg_lc for w in intent_schedule):
                preferred_next = current_flow
        # No saltar a confirmation desde reschedule por respuestas afirmativas genéricas
        if current_flow == "reschedule" and route.next == "confirmation":
            # En reagendamiento, NUNCA saltar al nodo de confirmación.
            # La confirmación del nuevo horario se realiza dentro del propio flujo de reagendamiento con reschedule_appointment.
            preferred_next = current_flow
    except Exception:
        pass
    print(f"🚦 Decisión del Supervisor: Ir a '{preferred_next}'")
    # Guardamos el flujo actual para saber a dónde volver después de una herramienta
    return {"next_agent": preferred_next, "current_flow": preferred_next}

# --- 3. Definición de Nodos de Lógica (Agentes Expertos) ---

def decide_after_agent(state: GlobalState) -> Literal["tools", "__end__"]:
    """Decide si ejecutar herramientas o terminar."""
    if isinstance(state["messages"][-1], AIMessage) and state["messages"][-1].tool_calls:
        return "tools"
    return "__end__"

# 3.1 Agente de Conocimiento (Pura Información)
knowledge_agent_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            """Eres un asistente de IA conversacional para SiluetSPA. Tu misión es responder preguntas sobre los servicios y también dudas generales de la empresa (horarios, ubicación, medios de pago, políticas, etc.).
**Instrucciones Clave:**
1.  **Analiza la conversación en `messages`.** El último mensaje es la pregunta más reciente del usuario.
 2.  **Si el último mensaje es una pregunta sobre servicios o dudas generales de la empresa**, usa la herramienta `knowledge_search` (con `organization_id` y, si no hay servicio específico, `service_id = null`) para obtener información precisa.
3.  **Si el historial ya contiene un `ToolMessage`**, normalmente basarás tu respuesta en ese resultado; **PERO** si la nueva pregunta se refiere a un **servicio diferente** al del último resultado (por nombre o sinónimos evidentes), realiza **una nueva llamada** a `knowledge_search` para ese servicio antes de responder.
4.  **NO sugieras agendar una cita.** Tu trabajo es solo informar.

5.  Al invocar `knowledge_search`, NO agregues palabras clave adicionales ni entidades nuevas (p. ej., no agregues "SiluetSPA", "clínica estética", etc.).
    - Puedes normalizar levemente el texto del usuario (minúsculas, quitar signos, corrección menor) o parafrasear de forma breve SIN introducir nuevos conceptos.
    - Mantén el idioma original de la pregunta y su intención.
    - Usa únicamente `{organization_id}` y, si aplica, `service_id` tal cual vengan en el contexto.

**Contexto:** {context_block}

**Variables para herramientas (multitenancy):**
- organization_id: {organization_id}
- contact_id: {contact_id}
- phone: {phone}
- phone_number: {phone_number}
- country_code: {country_code}

Al invocar herramientas, usa estos valores exactamente para los parámetros correspondientes.

**Estilo de comunicación:**
- Breve, claro y cercano en español neutro, usando tuteo.
- Incluye 1–2 emojis sutiles y pertinentes al tema (p. ej., 🙂, 💡, 📌). No abuses.
- Usa listas cuando presentes varias opciones o pasos.
- Mantén un tono empático.

**Reglas de veracidad (obligatorias):**
- **Solo proporciona información que esté explícitamente en los resultados de `knowledge_search`**. No inventes ni supongas datos.
- **No ofrezcas proporcionar información o servicios que no puedas cumplir**. Si no tienes un dato, di que no está disponible.
- **Limítate a responder lo preguntado** sin añadir ofertas o sugerencias no solicitadas.
- No afirmes acciones que no realizaste. Este nodo solo informa, no ejecuta acciones.

**Regla de relevancia y escalamiento:**
- Responde únicamente con información encontrada en `knowledge_search`.
- Si no tienes la información solicitada o hay un error, pregunta: "¿Te gustaría hablar con un asesor?".
- Solo llama `escalate_to_human` DESPUÉS de confirmación explícita del usuario.
"""
        ),
        MessagesPlaceholder(variable_name="messages"),
    ]
)
knowledge_agent_runnable = knowledge_agent_prompt | llm.bind_tools([knowledge_search])

async def knowledge_node(state: GlobalState) -> Dict[str, Any]:
    print("--- 📚 NODO: Conocimiento (Informativo) ---")
    response = await knowledge_agent_runnable.ainvoke(
        {
            "context_block": state.get("context_block", "No hay resumen."),
            "messages": state["messages"],
            "organization_id": state.get("organization_id"),
            "contact_id": state.get("contact_id"),
            "phone": state.get("phone"),
            "phone_number": state.get("phone_number"),
            "country_code": state.get("country_code"),
            "chat_identity_id": state.get("chat_identity_id"),
        }
    )
    # Log de tool calls/respuesta
    try:
        if isinstance(response, AIMessage) and getattr(response, "tool_calls", None):
            calls_summary = [
                {"name": c.get("name"), "args": c.get("args")} for c in response.tool_calls
            ]
            print(f"🧰 (knowledge) Llamadas a herramientas: {json.dumps(calls_summary, ensure_ascii=False)}")
        else:
            print(f"🗣️ (knowledge) Respuesta directa: {getattr(response, 'content', '')[:300]}")
    except Exception:
        pass
    return {"messages": [response]}

# 3.2 Agente de Agendamiento (Gestor de Citas)
from .tools import (
    resolve_contact_on_booking,
    resolve_relative_date,
    find_appointment_for_cancellation,
    get_upcoming_user_appointments,
    find_appointment_for_update,
    confirm_appointment,
    reschedule_appointment,
    link_chat_identity_to_contact,
)
appointment_tools = [
    knowledge_search,
    update_service_in_state,
    resolve_relative_date,
    check_availability,
    select_appointment_slot,
    book_appointment,
    resolve_contact_on_booking,
    link_chat_identity_to_contact,
    get_user_appointments,
    cancel_appointment,
]
appointment_agent_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            """Eres un agente experto en agendar citas. Tu objetivo es guiar al usuario a través del proceso de reserva.

**FASE ACTUAL (revisa `service_id` y `pending_assessment_service` para saber en qué fase estás):**

- **FASE ESPECIAL: Servicio requiere valoración (si `pending_assessment_service` NO es NULO)**
  1. Informa al usuario que el servicio mencionado requiere una valoración previa (usa el nombre del servicio que está en `pending_assessment_service`).
  2. Pregunta: "¿Te gustaría agendar primero una cita de valoración?"
  3. Si acepta: usa `knowledge_search` con "valoración" o "consulta de valoración" para buscar el servicio de valoración.
  4. Una vez encontrado, usa `update_service_in_state` (con organization_id={organization_id}) con el service_id de la valoración. Después de guardar el servicio, procede inmediatamente a preguntar "¿Para qué fecha te gustaría agendar la valoración?" y continúa con el flujo de FASE 2.
  5. Si rechaza: di "Entiendo. Para agendar [servicio original] necesitarás primero una valoración. ¿Hay algo más en lo que pueda ayudarte?"

- **FASE 1: Identificación (si `service_id` es NULO y `pending_assessment_service` es NULO)**
  1. Si el último mensaje del usuario SOLO expresa intención genérica (p. ej., "quiero agendar"), primero PREGUNTA de forma clara: "¿Qué servicio te gustaría agendar?". NO uses herramientas todavía. No sugieras ejemplos ni inventes nombres de servicios.
  2. Solo si el ÚLTIMO mensaje contiene un nombre explícito de un servicio, usa `knowledge_search` para identificarlo y luego PIDE confirmación. No propongas nombres de servicios por tu cuenta.
  3. Tras confirmación explícita del usuario, usa `update_service_in_state` (con organization_id={organization_id}) para guardar el servicio.

- **FASE 2: Reserva (si `service_id` YA EXISTE y `pending_assessment_service` es NULO)**
  1. Tu misión es completar la reserva. NO busques servicios.
  2. IMPORTANTE: Si acabas de establecer el servicio con `update_service_in_state`, DEBES preguntar inmediatamente "¿Para qué fecha te gustaría agendar?" sin esperar otra interacción.
  3. Sigue estrictamente: Preguntar fecha -> `check_availability` (con `service_id`, `organization_id`, `check_date_str`) -> si hay slots, pedir hora y usar `select_appointment_slot` -> finalmente `book_appointment`.
  4. Nunca digas "no hay horarios" sin haber llamado antes a `check_availability` y sin haber verificado que la lista retornada esté vacía.
  5. Si el usuario expresa fechas relativas ("hoy", "mañana", "la otra semana", formatos DD/MM o DD-MM), primero usa `resolve_relative_date` con `timezone="America/Bogota"` para obtener `selected_date` en formato YYYY-MM-DD y luego llama a `check_availability`.
  6. **IMPORTANTE**: Al llamar `select_appointment_slot` DEBES pasar TRES parámetros obligatorios:
     - `appointment_date`: la fecha en formato YYYY-MM-DD
     - `start_time`: la hora de inicio en formato HH:MM
     - `available_slots`: DEBES pasar la lista completa de slots exactamente como la tienes en el estado (ver "Slots disponibles cargados" abajo)
     Si `available_slots` está vacío o no existe, primero vuelve a ejecutar `check_availability` y recién después llama a `select_appointment_slot`.

**Manejo de disponibilidad (sugerencias):**
- Si el día no tiene disponibilidad, informa claramente que ese día no hay horarios y pide al usuario elegir otra fecha.
- Si la hora pedida no está en `available_slots`, sugiere 3–5 horarios cercanos del mismo día usando `available_slots` y pide elegir uno de ellos.

**Cambio de servicio (cuando ya hay `service_id`):**
- Si el usuario menciona explícitamente otro servicio distinto al actual (por nombre o sinónimos) o rechaza el actual, primero CONFIRMA: "¿Deseas cambiar al servicio ‘X’?".
- Si confirma, usa `knowledge_search` para verificar el servicio y luego `update_service_in_state` con el nuevo `service_id` y `service_name`.
- Tras cambiar de servicio, considera el contexto volátil limpiado y vuelve a pedir fecha; continúa con `check_availability` -> `select_appointment_slot` -> `book_appointment`.

**Estado Actual:**
- Servicio: {service_name} (ID: {service_id})
- Servicio pendiente de valoración: {pending_assessment_service}
- Fecha: {selected_date}
- Hora: {selected_time}
- Slots disponibles cargados: {available_slots}
**Contexto:** {context_block}

**Variables para herramientas (multitenancy):**
- organization_id: {organization_id}
- contact_id: {contact_id}
- phone: {phone}
- phone_number: {phone_number}
- country_code: {country_code}
- chat_identity_id: {chat_identity_id}

Al invocar herramientas, usa estos valores exactamente para los parámetros correspondientes.

Si `contact_id` es nulo y necesitas agendar, primero llama a `resolve_contact_on_booking` con `organization_id`, `phone_number` y `country_code` para obtener/crear el contacto.
Si `contact_id` YA existe, NO llames `resolve_contact_on_booking` y NO digas que vas a verificar el contacto.

Si `resolve_contact_on_booking` devuelve que faltan nombres, PIDE nombre y apellido al usuario y vuelve a llamar a `resolve_contact_on_booking` pasando `first_name` y `last_name`. Tras obtener `contact_id`, llama a `link_chat_identity_to_contact(chat_identity_id={chat_identity_id}, organization_id={organization_id}, contact_id=<id>)` para persistir el enlace.

**Estilo de comunicación:**
- Cercano, amable y proactivo. Mensajes cortos y claros.
- Usa 1–2 emojis sutiles y pertinentes, p. ej., 📅, 🙂, ✅, ⏰. No abuses.
- Cuando pidas elegir fecha/hora, ofrece ejemplos o opciones concretas.
- Confirma pasos importantes con frases breves.

**Reglas de veracidad (obligatorias):**
- No inventes horarios ni estados de reserva.
- No afirmes que agendaste si no ejecutaste `book_appointment` con éxito.
- Si no hay disponibilidad, dilo y ofrece alternativas.
- No inventes ni sugieres nombres de servicios. Si el usuario no especifica, solo pregunta cuál desea.
"""
        ),
        MessagesPlaceholder(variable_name="messages"),
    ]
)
appointment_agent_runnable = appointment_agent_prompt | llm.bind_tools(appointment_tools)

async def appointment_node(state: GlobalState) -> Dict[str, Any]:
    print("--- 📅 NODO: Agendamiento (Agente Experto) ---")
    # Estado actual resumido
    print(json.dumps({
        "service_id": state.get("service_id"),
        "service_name": state.get("service_name"),
        "selected_date": state.get("selected_date"),
        "selected_time": state.get("selected_time"),
        "available_slots_len": len(state.get("available_slots") or [])
    }, ensure_ascii=False))
    last_user_message = ""
    for m in reversed(state["messages"]):
        if isinstance(m, HumanMessage):
            last_user_message = m.content
            break

    response = await appointment_agent_runnable.ainvoke(
        {
            "service_id": state.get("service_id"),
            "service_name": state.get("service_name"),
            "selected_date": state.get("selected_date"),
            "selected_time": state.get("selected_time"),
            "available_slots": state.get("available_slots"),
            "pending_assessment_service": state.get("pending_assessment_service"),
            "context_block": state.get("context_block", "No hay resumen."),
            "messages": state["messages"],
            "organization_id": state.get("organization_id"),
            "contact_id": state.get("contact_id"),
            "phone": state.get("phone"),
            "phone_number": state.get("phone_number"),
            "country_code": state.get("country_code"),
            "chat_identity_id": state.get("chat_identity_id"),
            "last_user_message": last_user_message,
        }
    )
    # Log de tool calls si existen
    try:
        if isinstance(response, AIMessage) and getattr(response, "tool_calls", None):
            calls_summary = [
                {"name": c.get("name"), "args": c.get("args")} for c in response.tool_calls
            ]
            print(f"🧰 Llamadas a herramientas: {json.dumps(calls_summary, ensure_ascii=False)}")
        else:
            # Respuesta directa
            print(f"🗣️ Respuesta directa del agente: {getattr(response, 'content', '')[:300]}")
    except Exception:
        pass
    return {"messages": [response]}

async def cancellation_node(state: GlobalState) -> Dict[str, Any]:
    print("--- ❌ NODO: Cancelación ---")
    print(f"[cancel] contact_id actual: {state.get('contact_id')}")
    prompt = ChatPromptTemplate.from_messages([
        ("system", """
Eres un asistente para cancelar citas. Flujo inteligente:

**PASO 0 - CRÍTICO: Resolver contacto si no existe**
- Si `contact_id` es NULO o None, DEBES PRIMERO llamar a `resolve_contact_on_booking(organization_id={organization_id}, phone_number={phone_number}, country_code={country_code})` para obtener el UUID del contacto.
- NUNCA uses el número de teléfono como contact_id. El contact_id es un UUID que obtienes de `resolve_contact_on_booking`.
- Solo después de tener el contact_id UUID válido, procede con los siguientes pasos.

1) Si el usuario NO menciona fecha: PIDE la fecha de la cita a cancelar. Ofrece listar citas futuras si no la recuerda.
2) **CRÍTICO**: Si menciona CUALQUIER fecha relativa (hoy, mañana, pasado mañana, esta semana, próxima semana, DD/MM, DD-MM), DEBES PRIMERO usar `resolve_relative_date(date_str="<exactamente lo que dijo el usuario>", timezone="America/Bogota")` para obtener `selected_date` en formato YYYY-MM-DD.
   - NUNCA intentes adivinar fechas. SIEMPRE usa `resolve_relative_date` primero.
3) Solo DESPUÉS de tener `selected_date` resuelto, si NO hay hora, intenta `find_appointment_for_cancellation(contact_id, selected_date)` usando el UUID:
   - 0 resultados: informa y ofrece listar próximas citas.
   - 1 resultado: PIDE confirmación para cancelar esa cita.
   - >1 resultado: PIDE la hora exacta para desambiguar.
4) Si hay fecha y hora, llama `find_appointment_for_cancellation` con ambos y, si hay una sola, llama `cancel_appointment` y confirma.
5) Si el usuario no recuerda fecha, usa `get_upcoming_user_appointments(contact_id)` con el UUID para listar y pide que elija una.

Responde breve y en segunda persona. No inventes datos. Usa herramientas cuando corresponda.

 Manejo de errores:
 - Si alguna herramienta falla o devuelve un error, informa que no pudiste completar la cancelación en este momento y pregunta "¿Te gustaría hablar con un asesor?".
 - Solo llama `escalate_to_human` DESPUÉS de que el usuario confirme explícitamente que quiere hablar con un asesor.

**Reglas de veracidad (obligatorias):**
- No inventes estados de citas ni confirmes cancelaciones sin haber llamado `cancel_appointment` con éxito.
- No digas que consultaste citas si no ejecutaste la herramienta correspondiente.

**Estilo de comunicación:**
- Empático y claro, con 1–2 emojis sutiles (p. ej., 🗓️, ⚠️, ✅). No abuses.
- Propón opciones concretas para facilitar la elección.

**Resolución de contacto (multitenancy):**
- Si `contact_id` es NULO, primero llama a `resolve_contact_on_booking(organization_id={organization_id}, phone_number={phone_number}, country_code={country_code})` para obtener/crear el contacto en CRM y usar su `contact_id` en las demás herramientas.
- Si `contact_id` YA existe, NO llames `resolve_contact_on_booking` ni indiques que vas a verificar el contacto.
- Solo si la herramienta indica que faltan nombres (contacto inexistente), pide nombre y apellido al usuario y vuelve a llamar pasando `first_name` y `last_name` junto con `organization_id`, `phone_number` y `country_code` del contexto para CREAR el contacto. Luego, enlaza el contacto al hilo con `link_chat_identity_to_contact(chat_identity_id={chat_identity_id}, organization_id={organization_id}, contact_id=<id>)`.

**Regla clave (teléfono):**
- Nunca pidas el número de teléfono al usuario. Usa siempre `phone_number` y `country_code` del contexto para `resolve_contact_on_booking`.
"""),
        MessagesPlaceholder("messages")
    ])
    tools_for_cancel = [resolve_relative_date, find_appointment_for_cancellation, get_upcoming_user_appointments, cancel_appointment]
    # Gating dinámico: solo exponer resolución de contacto si no hay contact_id
    if not state.get("contact_id"):
        tools_for_cancel = [resolve_contact_on_booking, link_chat_identity_to_contact] + tools_for_cancel
    runnable = prompt | llm.bind_tools(tools_for_cancel)
    if os.getenv("LOG_VERBOSE", "false").lower() in ("1", "true", "yes"):
        print("[cancel] Últimos mensajes:")
        try:
            for m in state["messages"][-6:]:
                role = type(m).__name__
                print(f"  - {role}: {getattr(m, 'content', '')[:200]}")
        except Exception:
            pass
    response = await runnable.ainvoke({
        "messages": state["messages"],
        "organization_id": state.get("organization_id"),
        "contact_id": state.get("contact_id"),
        "phone_number": state.get("phone_number"),
        "country_code": state.get("country_code"),
        "chat_identity_id": state.get("chat_identity_id"),
    })
    try:
        if isinstance(response, AIMessage) and getattr(response, "tool_calls", None):
            calls_summary = [{"name": c.get("name"), "args": c.get("args")} for c in response.tool_calls]
            print(f"🧰 (cancel) tool_calls: {json.dumps(calls_summary, ensure_ascii=False)}")
        else:
            print(f"🗣️ (cancel) respuesta directa: {getattr(response, 'content', '')[:300]}")
    except Exception:
        pass
    return {"messages": [response]}

async def confirmation_node(state: GlobalState) -> Dict[str, Any]:
    print("--- ✅ NODO: Confirmación ---")
    print(f"[confirm] contact_id actual: {state.get('contact_id')}")
    prompt = ChatPromptTemplate.from_messages([
        ("system", """
Eres un asistente para confirmar citas. Flujo inteligente:

**PASO 0 - CRÍTICO: Resolver contacto si no existe**
- Si `contact_id` es NULO o None, DEBES PRIMERO llamar a `resolve_contact_on_booking(organization_id={organization_id}, phone_number={phone_number}, country_code={country_code})` para obtener el UUID del contacto.
- NUNCA uses el número de teléfono como contact_id. El contact_id es un UUID que obtienes de `resolve_contact_on_booking`.
- Solo después de tener el contact_id UUID válido, procede con los siguientes pasos.

1) Si el usuario no da fecha/hora, pide fecha. 
2) **CRÍTICO**: Si el usuario menciona CUALQUIER fecha relativa (hoy, mañana, pasado mañana, esta semana, próxima semana, DD/MM, DD-MM), DEBES PRIMERO usar `resolve_relative_date(date_str="<exactamente lo que dijo el usuario>", timezone="America/Bogota")` y guardar el resultado como `selected_date`.
   - NUNCA intentes adivinar o hardcodear fechas. SIEMPRE usa `resolve_relative_date` primero.
3) Solo DESPUÉS de tener `selected_date` resuelto (y hora si la da), usa `find_appointment_for_update(contact_id, date[, time])` usando el UUID.
   - 0 resultados: informa que no encontraste citas para esa fecha y ofrece listar próximas con `get_upcoming_user_appointments(contact_id)` con el UUID.
   - 1 resultado: confirma explícitamente si desea confirmar esa cita y llama `confirm_appointment(appointment_id)`.
   - >1 resultado: pide la hora exacta para desambiguar.
3) Tras confirmar, responde con un mensaje de confirmación con fecha y hora.

Responde breve y con 1 emoji sutil.

 Manejo de errores:
 - Si alguna herramienta falla o devuelve un error, informa que no pudiste completar la confirmación y pregunta "¿Te gustaría hablar con un asesor?".
 - Solo llama `escalate_to_human` DESPUÉS de que el usuario confirme explícitamente que quiere hablar con un asesor.

**Reglas de veracidad (obligatorias):**
- No inventes estados ni confirmes citas si no llamaste `confirm_appointment` con éxito.
- No digas que consultaste si no ejecutaste las herramientas correspondientes.

**Resolución de contacto (multitenancy):**
- Si `contact_id` es NULO, primero llama a `resolve_contact_on_booking(organization_id={organization_id}, phone_number={phone_number}, country_code={country_code})` para obtener/crear el contacto en CRM y usar su `contact_id` en las demás herramientas.
- Si `contact_id` YA existe, NO llames `resolve_contact_on_booking` ni indiques que vas a verificar el contacto.
- Solo si la herramienta indica que faltan nombres (contacto inexistente), pide nombre y apellido al usuario y vuelve a llamar pasando `first_name` y `last_name` junto con `organization_id`, `phone_number` y `country_code` del contexto para CREAR el contacto. Luego, enlaza el contacto al hilo con `link_chat_identity_to_contact(chat_identity_id={chat_identity_id}, organization_id={organization_id}, contact_id=<id>)`.

**Regla clave (teléfono):**
- Nunca pidas el número de teléfono al usuario. Usa siempre `phone_number` y `country_code` del contexto para `resolve_contact_on_booking`.
"""),
        MessagesPlaceholder("messages")
    ])
    tools_for_confirm = [resolve_relative_date, find_appointment_for_update, get_upcoming_user_appointments, confirm_appointment]
    if not state.get("contact_id"):
        tools_for_confirm = [resolve_contact_on_booking, link_chat_identity_to_contact] + tools_for_confirm
    runnable = prompt | llm.bind_tools(tools_for_confirm)
    if os.getenv("LOG_VERBOSE", "false").lower() in ("1", "true", "yes"):
        print("[confirm] Últimos mensajes:")
        try:
            for m in state["messages"][-6:]:
                role = type(m).__name__
                print(f"  - {role}: {getattr(m, 'content', '')[:200]}")
        except Exception:
            pass
    response = await runnable.ainvoke({
        "messages": state["messages"],
        "organization_id": state.get("organization_id"),
        "contact_id": state.get("contact_id"),
        "phone_number": state.get("phone_number"),
        "country_code": state.get("country_code"),
        "chat_identity_id": state.get("chat_identity_id"),
    })
    try:
        if isinstance(response, AIMessage) and getattr(response, "tool_calls", None):
            calls_summary = [{"name": c.get("name"), "args": c.get("args")} for c in response.tool_calls]
            print(f"🧰 (confirm) tool_calls: {json.dumps(calls_summary, ensure_ascii=False)}")
        else:
            print(f"🗣️ (confirm) respuesta directa: {getattr(response, 'content', '')[:300]}")
    except Exception:
        pass
    return {"messages": [response]}

async def reschedule_node(state: GlobalState) -> Dict[str, Any]:
    print("--- 🔁 NODO: Reagendamiento ---")
    print(f"[reschedule] contact_id actual: {state.get('contact_id')}")
    # Debug completo del estado
    print(f"[reschedule] 🔍 Estado completo de slots:")
    print(f"  - available_slots en state: {state.get('available_slots') is not None}")
    print(f"  - Cantidad de slots: {len(state.get('available_slots', []))}")
    if state.get('available_slots'):
        print(f"  - Primeros 2 slots: {state.get('available_slots')[:2]}")
    prompt = ChatPromptTemplate.from_messages([
        ("system", """
Eres un asistente para reagendar citas. Sigue este orden ESTRICTO:

**PASO 0 - CRÍTICO: Resolver contacto si no existe**
- Si `contact_id` es NULO o None, DEBES PRIMERO llamar a `resolve_contact_on_booking(organization_id={organization_id}, phone_number={phone_number}, country_code={country_code})` para obtener el UUID del contacto.
- NUNCA uses el número de teléfono como contact_id. El contact_id es un UUID que obtienes de `resolve_contact_on_booking`.
- Solo después de tener el contact_id UUID válido, procede con los siguientes pasos.

**EJEMPLO DE FLUJO CORRECTO:**
Usuario: "quiero reagendar una cita que tengo para hoy a las 4pm"
1. Llamas: resolve_contact_on_booking(...) → obtienes contact_id UUID
2. Llamas: resolve_relative_date(date_str="hoy", timezone="America/Bogota") → obtienes "2025-01-14"  
3. Llamas: find_appointment_for_update(contact_id=<UUID>, date_str="2025-01-14", time_str="16:00")

1) Ubicar la cita actual (obligatorio antes de pedir nueva fecha/hora):
   - IMPORTANTE: Usa el contact_id UUID (NO el número de teléfono) en todas las herramientas.
   - **CRÍTICO**: Si el usuario menciona fechas relativas como "hoy", "mañana", "pasado mañana", "esta semana", "la próxima semana", o fechas en formato DD/MM o DD-MM, DEBES PRIMERO llamar a `resolve_relative_date(date_str="<lo que dijo el usuario>", timezone="America/Bogota")` para obtener la fecha en formato YYYY-MM-DD.
   - NUNCA intentes adivinar o hardcodear fechas. SIEMPRE usa `resolve_relative_date` para fechas relativas.
   - Si el usuario no recuerda la fecha, ofrece listar con `get_upcoming_user_appointments(contact_id)` donde contact_id es el UUID obtenido.
   - Solo después de tener la fecha resuelta (YYYY-MM-DD), usa `find_appointment_for_update(contact_id, date[, time])` con el UUID.
   - Cuando haya exactamente una cita, considera la cita IDENTIFICADA.

2) Pedir nueva fecha/hora:
   - Una vez identificada la cita, **primero confirma con el usuario** la cita que se va a cambiar (ej: "Entendido, vamos a reagendar tu cita de [Servicio] para el [Fecha] a las [Hora].").
   - **Inmediatamente después**, pregunta por la nueva fecha deseada (ej: "¿Para qué nueva fecha te gustaría moverla?").
   - NO uses la fecha de la cita original para buscar disponibilidad. Debes obtener una fecha nueva del usuario.
   - Cuando el usuario proporcione la nueva fecha, usa `resolve_relative_date` si es necesario, y luego `check_availability`.

3) Ejecutar el cambio:
   - Llama `reschedule_appointment(appointment_id, new_date, new_start_time, member_id, comment?)` y confirma.

Sé claro y breve, con 1 emoji.

Manejo de errores:
- Si alguna herramienta falla o devuelve un error, informa que no pudiste completar el reagendamiento y pregunta "¿Te gustaría hablar con un asesor?". 
- Solo llama `escalate_to_human` DESPUÉS de que el usuario confirme explícitamente que quiere hablar con un asesor.

**Resolución de contacto (multitenancy):**
- Si `contact_id` es NULO, primero llama a `resolve_contact_on_booking(organization_id={organization_id}, phone_number={phone_number}, country_code={country_code})`.
- El resultado te dará un contact_id UUID que debes usar en todas las herramientas siguientes.
- Si falta nombre/apellido, pídelos y vuelve a llamar pasando `first_name` y `last_name`. Luego, enlaza con `link_chat_identity_to_contact(chat_identity_id={chat_identity_id}, organization_id={organization_id}, contact_id=<uuid>)`.

**Regla clave (teléfono):**
- Nunca pidas el número de teléfono al usuario. Usa `phone_number` y `country_code` del contexto.

**Regla de slots:**
- Ejecuta `check_availability` antes de `select_appointment_slot`.
- Al llamar `select_appointment_slot`, pasa `available_slots` EXACTAMENTE como lo devolvió `check_availability`.
- Si `available_slots` falta o está vacío, ejecuta `check_availability` y luego `select_appointment_slot`.
- **IMPORTANTE**: Si ya tienes `available_slots` cargados (se muestran abajo), NO vuelvas a llamar `check_availability`. Usa directamente `select_appointment_slot` con los slots existentes.

**Estado actual de slots disponibles:**
- Slots cargados: {available_slots}
- Si los slots ya están cargados y el usuario elige una hora (ej: "2:30 PM", "14:30"), llama directamente `select_appointment_slot` con los slots existentes.

**Falta de disponibilidad:**
- Si el día no tiene horarios, dilo y pide otro día.
- Si la hora pedida no está, sugiere 3–5 horarios cercanos del mismo día.

**REGLA DE VERACIDAD (MUY IMPORTANTE):**
- **NUNCA** confirmes un reagendamiento si la herramienta `reschedule_appointment` no ha sido llamada y ha devuelto `success: True`.
- Es una falta grave inventar una confirmación. Si no estás seguro, informa que no pudiste completar la acción y pregunta si el usuario desea intentar de nuevo o hablar con un asesor.
"""),
        MessagesPlaceholder("messages")
    ])
    # Base: localizar cita actual primero; no exponer disponibilidad hasta identificar
    tools_for_res = [resolve_relative_date, find_appointment_for_update, get_upcoming_user_appointments]
    # Habilitar resolución de contacto sólo si falta
    if not state.get("contact_id"):
        tools_for_res = [resolve_contact_on_booking, link_chat_identity_to_contact] + tools_for_res
    # Exponer check_availability sólo si hay cita identificada (focused_appointment o service_id)
    if state.get("focused_appointment") and state.get("selected_date"):
        tools_for_res.append(check_availability)
    # Exponer select_appointment_slot sólo si ya hay available_slots en el estado
    if state.get("available_slots"):
        tools_for_res.append(select_appointment_slot)
    # Si el usuario confirma explícitamente una hora presente en available_slots, el agente DEBE seleccionar ese slot en vez de escalar o confirmar
    # Gating: permitir reschedule_appointment solo cuando haya fecha/hora/miembro y cita identificada
    if (
        state.get("selected_date") and state.get("selected_time") and state.get("selected_member_id") and 
        (state.get("focused_appointment") or state.get("service_id"))
    ):
        tools_for_res.append(reschedule_appointment)
    runnable = prompt | llm.bind_tools(tools_for_res)
    if os.getenv("LOG_VERBOSE", "false").lower() in ("1", "true", "yes"):
        print("[reschedule] Últimos mensajes:")
        try:
            for m in state["messages"][-6:]:
                role = type(m).__name__
                print(f"  - {role}: {getattr(m, 'content', '')[:200]}")
        except Exception:
            pass
    # Debug: mostrar el estado actual de los slots
    print(f"[reschedule] Estado actual - available_slots: {len(state.get('available_slots') or [])} slots, selected_date: {state.get('selected_date')}, selected_time: {state.get('selected_time')}, selected_member_id: {state.get('selected_member_id')}, service_id: {state.get('service_id')}, focused_appointment: {bool(state.get('focused_appointment'))}")
    print(f"[reschedule] Herramientas disponibles: {[t.__name__ if hasattr(t, '__name__') else str(t) for t in tools_for_res]}")
    
    response = await runnable.ainvoke({
        "messages": state["messages"],
        "organization_id": state.get("organization_id"),
        "contact_id": state.get("contact_id"),
        "phone_number": state.get("phone_number"),
        "country_code": state.get("country_code"),
        "chat_identity_id": state.get("chat_identity_id"),
        "available_slots": state.get("available_slots"),
        "selected_date": state.get("selected_date"),
        "focused_appointment": state.get("focused_appointment"),
    })
    try:
        if isinstance(response, AIMessage) and getattr(response, "tool_calls", None):
            calls_summary = [{"name": c.get("name"), "args": c.get("args")} for c in response.tool_calls]
            print(f"🧰 (reschedule) tool_calls: {json.dumps(calls_summary, ensure_ascii=False)}")
        else:
            print(f"🗣️ (reschedule) respuesta directa: {getattr(response, 'content', '')[:300]}")
    except Exception:
        pass
    return {"messages": [response]}

async def escalation_node(state: GlobalState) -> Dict[str, Any]:
    print("--- 🔴 NODO: Escalamiento ---")
    print(f"[escalation] contact_id actual: {state.get('contact_id')}")
    
    # Prompt específico para el escalamiento
    prompt = ChatPromptTemplate.from_messages([
        ("system", f"""
Eres un asistente de escalamiento. El usuario ha solicitado hablar con un asesor humano o hay un problema que requiere intervención humana.

Tu trabajo es:
1. Confirmar la solicitud de escalamiento 
2. Llamar a `escalate_to_human` con los datos del contexto
3. Informar al usuario que un asesor ha sido notificado

**Contexto del usuario:**
- Organization ID: {{organization_id}}
- Chat Identity ID: {{chat_identity_id}} 
- Phone Number: {{phone_number}}
- Country Code: {{country_code}}

Responde de manera empática y profesional. Usa 1 emoji.
"""),
        MessagesPlaceholder("messages")
    ])
    
    tools_for_escalation = [escalate_to_human]
    runnable = prompt | llm.bind_tools(tools_for_escalation)
    
    response = await runnable.ainvoke({
        "messages": state["messages"],
        "organization_id": state.get("organization_id"),
        "chat_identity_id": state.get("chat_identity_id"),
        "phone_number": state.get("phone_number"),
        "country_code": state.get("country_code"),
    })
    
    try:
        if isinstance(response, AIMessage) and getattr(response, "tool_calls", None):
            calls_summary = [{"name": c.get("name"), "args": c.get("args")} for c in response.tool_calls]
            print(f"🧰 (escalation) tool_calls: {json.dumps(calls_summary, ensure_ascii=False)}")
        else:
            print(f"🗣️ (escalation) respuesta directa: {getattr(response, 'content', '')[:300]}")
    except Exception:
        pass
    
    return {"messages": [response]}


# --- 4. Construcción del Grafo ---
workflow = StateGraph(GlobalState)

workflow.add_node("supervisor", supervisor_node)
workflow.add_node("knowledge", knowledge_node)
workflow.add_node("appointment", appointment_node)
workflow.add_node("cancellation", cancellation_node)
workflow.add_node("confirmation", confirmation_node)
workflow.add_node("reschedule", reschedule_node)
workflow.add_node("escalation", escalation_node)

async def apply_tool_effects(state: GlobalState) -> Dict[str, Any]:
    """Aplica efectos en el estado a partir del último ToolMessage si es estructurado."""
    print("--- 🔧 NODO: Aplicar efectos de herramientas ---")
    if not state["messages"]:
        print("🔧 No hay mensajes en el estado")
        return {}
    last_msg = state["messages"][-1]
    print(f"🔧 Tipo del último mensaje: {type(last_msg).__name__}")
    if not isinstance(last_msg, ToolMessage):
        print("🔧 El último mensaje no es un ToolMessage")
        return {}

    payload = None
    print(f"🔧 Contenido del ToolMessage (tipo): {type(last_msg.content)}")
    print(f"🔧 Contenido del ToolMessage (primeros 200 chars): {str(last_msg.content)[:200]}")
    
    try:
        # Intentar decodificar si es un string JSON
        if isinstance(last_msg.content, str):
            try:
                payload = json.loads(last_msg.content)
                print(f"🔧 Payload decodificado desde JSON string")
            except json.JSONDecodeError:
                # Podría ser un string Pydantic, intentar parsear
                content_str = str(last_msg.content)
                if "success=" in content_str and "message=" in content_str:
                    # Es un objeto Pydantic serializado, extraer campos
                    print("🔧 Detectado formato Pydantic, parseando campos...")
                    payload = {}
                    import re
                    # Parsear campos del formato Pydantic
                    for match in re.finditer(r"(\w+)=(['\"])([^'\"]*)\2|(\w+)=(True|False|None|\d+)", content_str):
                        if match.group(1):  # Campo con string
                            payload[match.group(1)] = match.group(3)
                        elif match.group(4):  # Campo booleano/None/número
                            value = match.group(5)
                            if value == "True":
                                payload[match.group(4)] = True
                            elif value == "False":
                                payload[match.group(4)] = False
                            elif value == "None":
                                payload[match.group(4)] = None
                            else:
                                payload[match.group(4)] = int(value) if value.isdigit() else value
                    if payload:
                        print(f"🔧 Payload parseado desde Pydantic: {json.dumps(payload, ensure_ascii=False)}")
                    else:
                        print("🔧 No se pudo parsear formato Pydantic")
                        return {}
                else:
                    # Si falla, es probable que sea un string plano, lo ignoramos para efectos de estado
                    print("🔧 No se pudo decodificar JSON del string")
                    return {}
        # Si ya es dict o list, lo usamos directamente
        elif isinstance(last_msg.content, (dict, list)):
            payload = last_msg.content
            print(f"🔧 Payload ya es dict/list")
        else:
            # Otros tipos no se procesan para efectos de estado
            print(f"🔧 Tipo de contenido no procesable: {type(last_msg.content)}")
            return {}
    except Exception as e:
        print(f"🔧 Error procesando payload: {e}")
        return {}

    tool_name = getattr(last_msg, "name", None)
    print(f"🔧 Nombre de la herramienta: {tool_name}")
    if not tool_name or payload is None:
        print(f"🔧 Sin tool_name o payload es None")
        return {}

    updates: Dict[str, Any] = {}

    # Manejo específico por herramienta
    if tool_name == "update_service_in_state" and isinstance(payload, dict):
        if payload.get("action") == "requires_assessment":
            # El servicio requiere valoración previa
            updates["pending_assessment_service"] = {
                "service_id": payload.get("original_service_id"),
                "service_name": payload.get("original_service_name"),
                "message": payload.get("message")
            }
            # Limpiar el contexto para evitar confusión
            updates["service_id"] = None
            updates["service_name"] = None
            updates["available_slots"] = None
            updates["selected_date"] = None
            updates["selected_time"] = None
            updates["selected_member_id"] = None
        elif payload.get("action") == "update_service":
            updates["service_id"] = payload.get("service_id")
            updates["service_name"] = payload.get("service_name")
            # Al cambiar servicio, limpiar contexto volátil
            updates["available_slots"] = None
            updates["selected_date"] = None
            updates["selected_time"] = None
            updates["selected_member_id"] = None
            updates["ready_to_book"] = None
            # Si había un pending_assessment_service, limpiarlo
            updates["pending_assessment_service"] = None
    elif tool_name == "resolve_relative_date" and isinstance(payload, dict):
        if payload.get("selected_date"):
            updates["selected_date"] = payload.get("selected_date")
    elif tool_name == "resolve_contact_on_booking" and isinstance(payload, dict):
        # Si resolvió/creó contacto, mantenerlo en estado para pasos siguientes
        if payload.get("success") and payload.get("contact_id"):
            updates["contact_id"] = payload.get("contact_id")
    elif tool_name == "check_availability":
        slots = []
        if isinstance(payload, dict):
            if payload.get("success") and "available_slots" in payload:
                slots = payload["available_slots"]
            else:
                slots = payload.get("available_slots", [])
        elif isinstance(payload, list):
            slots = payload
        updates["available_slots"] = slots
        print(f"📦 available_slots actualizados: {len(slots)} slots")
        print(f"📦 Ejemplo de slots: {slots[:2] if slots else 'Sin slots'}")  # Mostrar primeros 2 slots como debug
    elif tool_name == "find_appointment_for_update" and isinstance(payload, dict):
        # Guardar cita enfocada y service_id para habilitar el resto del flujo
        if payload.get("success") and (payload.get("appointment_id") or payload.get("candidates")):
            updates["focused_appointment"] = payload
            if payload.get("service_id"):
                updates["service_id"] = payload["service_id"]
    elif tool_name == "select_appointment_slot" and isinstance(payload, dict):
        # NO eliminar available_slots cuando falla, el agente necesita intentar de nuevo
        if payload.get("success") is not False:
            # Solo actualizar si la selección fue exitosa
            if payload.get("selected_date"):
                updates["selected_date"] = payload.get("selected_date")
            if payload.get("selected_time"):
                updates["selected_time"] = payload.get("selected_time")
            if payload.get("member_id"):
                updates["selected_member_id"] = payload.get("member_id")

    # Fallback genérico para cargas simples que traen selected_*
    if not updates and isinstance(payload, dict):
        if payload.get("selected_date"):
            updates["selected_date"] = payload.get("selected_date")
        if payload.get("selected_time"):
            updates["selected_time"] = payload.get("selected_time")
        if payload.get("member_id"):
            updates["selected_member_id"] = payload.get("member_id")

    return updates

# Nuevo nodo que ejecuta la herramienta Y aplica sus efectos
async def tool_executor_node(state: GlobalState) -> Dict[str, Any]:
    """Ejecuta la herramienta y luego aplica sus efectos en el estado."""
    print("--- ⚙️ NODO: Ejecutor de Herramientas ---")
    
    # 1. Ejecutar el ToolNode estándar para invocar la herramienta
    tool_node = ToolNode(all_tools)
    tool_result = await tool_node.ainvoke(state)
    
    # El resultado de ToolNode es un diccionario con 'messages': [ToolMessage]
    # Lo fusionamos de nuevo al estado para que apply_tool_effects pueda leerlo
    new_state_for_effects = state.copy()
    new_state_for_effects["messages"] = list(new_state_for_effects["messages"]) + list(tool_result["messages"])
    
    # 2. Aplicar los efectos de la herramienta al estado
    state_after_effects = await apply_tool_effects(new_state_for_effects)

    # Devolvemos un diccionario que LangGraph puede fusionar de nuevo al estado principal
    # Incluye tanto el ToolMessage como las actualizaciones de estado de apply_tool_effects
    final_updates = {}
    final_updates.update(tool_result) # {"messages": [ToolMessage(...)]}
    final_updates.update(state_after_effects) # {"available_slots": [...]}
    
    # Debug: mostrar qué actualizaciones se están aplicando
    if state_after_effects:
        print(f"⚙️ Actualizaciones de estado aplicadas: {list(state_after_effects.keys())}")
        if "available_slots" in state_after_effects and state_after_effects["available_slots"] is not None:
            print(f"⚙️ available_slots tiene {len(state_after_effects['available_slots'])} elementos")
    
    return final_updates

workflow.add_node("tools", tool_executor_node)


# --- 5. Lógica de Enrutamiento (Edges) ---

workflow.set_entry_point("supervisor")

workflow.add_conditional_edges(
    "supervisor",
    lambda state: state["next_agent"],
    {
        "knowledge": "knowledge",
        "appointment": "appointment",
        "cancellation": "cancellation",
        "confirmation": "confirmation",
        "reschedule": "reschedule",
        "escalation": "escalation",
        "__end__": END
    }
)

workflow.add_conditional_edges("knowledge", decide_after_agent)
workflow.add_conditional_edges("appointment", decide_after_agent)
workflow.add_conditional_edges("cancellation", decide_after_agent)
workflow.add_conditional_edges("confirmation", decide_after_agent)
workflow.add_conditional_edges("reschedule", decide_after_agent)
workflow.add_conditional_edges("escalation", decide_after_agent)

workflow.add_edge("tools", "supervisor")

# --- 6. Compilación y FastAPI ---
# Eliminado redis_url; no se usa checkpointer Redis

# La app FastAPI y estados de runtime
app = FastAPI()

class MessageSerializer:
    def dumps(self, obj):
        return json.dumps(dumps(obj), ensure_ascii=False)

    def loads(self, s):
        return loads(json.loads(s))

@app.on_event("startup")
async def on_startup():
    # Usar siempre MemorySaver como checkpointer
    app.state.checkpointer = MemorySaver()
    print("🧠 Checkpointer en memoria (MemorySaver)")
    # Compilar el grafo con el checkpointer elegido
    app.state.app_graph = workflow.compile(checkpointer=app.state.checkpointer)

@app.on_event("shutdown")
async def on_shutdown():
    # Cerrar el context manager si existe
    _cm = getattr(app.state, "_cp_cm", None)
    if _cm is not None:
        try:
            await _cm.__aexit__(None, None, None)
        except Exception:
            pass

class InvokePayload(BaseModel):
    organizationId: str
    chatIdentityId: str
    contactId: Optional[str]
    phone: str
    phoneNumber: str
    countryCode: str
    firstName: Optional[str] = None
    message: str
    recentMessages: Optional[List[Dict[str, str]]] = None

 # app ya fue creado arriba

async def get_contact_data(contact_id: str, organization_id: str) -> Optional[Dict[str, Any]]:
    try:
        response = await run_db(lambda: supabase_client
                                .table('contacts')
                                .select('first_name, last_name')
                                .eq('id', contact_id)
                                .eq('organization_id', organization_id)
                                .maybe_single()
                                .execute())
        return response.data if response.data else None
    except Exception as e:
        print(f"❌ Error obteniendo datos del contacto {contact_id}: {e}")
        return None

@app.post("/invoke")
async def invoke(payload: InvokePayload, request: Request):
    print("🟢 /invoke payload recibido:")
    try:
        print(json.dumps({
            "organizationId": payload.organizationId,
            "chatIdentityId": payload.chatIdentityId,
            "contactId": payload.contactId,
            "phone": payload.phone,
            "phoneNumber": payload.phoneNumber,
            "countryCode": payload.countryCode,
            "message": payload.message
        }, ensure_ascii=False))
    except Exception:
        pass
    session_id = payload.chatIdentityId
    user_id = f"contact_{payload.contactId}" if payload.contactId else f"chat_{session_id}"
    
    try:
        first_name, last_name = payload.firstName or "Usuario", ""
        if payload.contactId:
            contact_data = await get_contact_data(payload.contactId, payload.organizationId)
            if contact_data:
                first_name = contact_data.get("first_name", "Usuario")
                last_name = contact_data.get("last_name", "")

        context_block = await sb_get_context_block(session_id)
        # Preferir historial reciente enviado por el gateway (Redis) si existe; fallback a Supabase
        recent_msgs = payload.recentMessages or []
        if recent_msgs:
            print(f"🗂️ Usando historial desde gateway (Redis): {len(recent_msgs)} mensajes")
            sb_history = recent_msgs[-6:]
        else:
            sb_history = await sb_get_last_messages(session_id, last_n=6)
        
        # Normalizador robusto usando deserialización oficial de LangChain
        from langchain_core.messages import HumanMessage, AIMessage
        from langchain_core.load import loads
        def normalize_to_message(m: Dict[str, Any]):
            # Caso 1: formato esperado { role, content }
            if isinstance(m, dict) and 'role' in m and 'content' in m:
                return AIMessage(content=m['content']) if m['role'] == 'assistant' else HumanMessage(content=m['content'])
            # Caso 2: dict serializado de LangChain ({ lc, type, id, kwargs: { content, type } })
            if isinstance(m, dict) and 'lc' in m and 'kwargs' in m:
                try:
                    # Usar deserialización oficial de LangChain
                    return loads(m)
                except Exception as e:
                    print(f"⚠️ Error deserializando mensaje LangChain: {e}")
                    # Fallback manual
                    content = m['kwargs'].get('content', '')
                    msg_type = m['kwargs'].get('type', 'human')
                    return AIMessage(content=content) if msg_type == 'ai' else HumanMessage(content=content)
            # Fallback: tratar como humano
            return HumanMessage(content=str(m))

        conversation_history = [normalize_to_message(m) for m in sb_history]
        # Evitar duplicar el último mensaje humano si ya viene en el historial
        if not (
            conversation_history
            and isinstance(conversation_history[-1], HumanMessage)
            and conversation_history[-1].content == payload.message
        ):
            conversation_history.append(HumanMessage(content=payload.message))
        
        # Debug: verificar que todos los mensajes son objetos Message válidos
        if os.getenv("LOG_VERBOSE", "false").lower() in ("1", "true", "yes"):
            print(f"🔍 Debug historial: {len(conversation_history)} mensajes")
            for i, msg in enumerate(conversation_history):
                print(f"  [{i}] {type(msg).__name__}: {msg.content[:50]}...")

        # Usar un namespace de checkpoint para evitar conflictos con estados previos incompatibles
        config = {
            "configurable": {"thread_id": session_id},
            "run_name": f"skytide_agent_{payload.organizationId}",
            "tags": [f"org:{payload.organizationId}", f"chat:{session_id}"]
        }

        # Eliminada validación/limpieza de estado Redis; con MemorySaver no aplica
        # Construir estado inicial sin sobreescribir `contact_id` si viene nulo
        initial_state_data: Dict[str, Any] = {
            "messages": conversation_history,
            "organization_id": payload.organizationId,
            "chat_identity_id": payload.chatIdentityId,
            "phone": payload.phone,
            "phone_number": payload.phoneNumber,
            "country_code": payload.countryCode,
            "context_block": context_block,
        }
        if payload.contactId:
            initial_state_data["contact_id"] = payload.contactId
        initial_state = GlobalState(**initial_state_data)
        
        # Ejecución asíncrona con checkpointer
        final_state_result = await app.state.app_graph.ainvoke(
            initial_state, {**config, "recursion_limit": 50}
        )

        ai_response_content = "No pude procesar tu solicitud."
        if final_state_result and final_state_result.get("messages"):
            # Intentamos extraer la mejor respuesta posible del último tramo del grafo
            extracted = _extract_final_ai_content(final_state_result["messages"]) or ""
            if extracted.strip():
                ai_response_content = extracted
            else:
                # Fallback adicional si no hay contenido directo
                last_message = final_state_result["messages"][-1]
                if isinstance(last_message, AIMessage):
                    ai_response_content = last_message.content

        # Log de salida del grafo
        try:
            print("🧾 Estado final (resumen):")
            print(json.dumps({
                "messages_len": len(final_state_result.get("messages", [])),
                "service_id": final_state_result.get("service_id"),
                "selected_date": final_state_result.get("selected_date"),
                "selected_time": final_state_result.get("selected_time"),
                "selected_member_id": final_state_result.get("selected_member_id"),
                "available_slots_len": len(final_state_result.get("available_slots") or [])
            }, ensure_ascii=False))
        except Exception:
            pass

        # Intentar actualizar el resumen del hilo de forma asíncrona (best-effort)
        try:
            # Ejecutar de forma no bloqueante para no añadir latencia a la respuesta
            async def _bg_update():
                try:
                    last_msgs = await sb_get_last_messages(session_id, last_n=12)
                    await _maybe_update_thread_summary(payload.organizationId, session_id, last_msgs)
                except Exception:
                    pass
            asyncio.create_task(_bg_update())
        except Exception:
            pass
        return {"response": ai_response_content}

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": "Internal server error."}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
