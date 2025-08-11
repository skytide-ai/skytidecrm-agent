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
- `escalation`: Si el usuario está frustrado, pide hablar con un asesor o el sistema falla repetidamente.
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
        return {"next_agent": state['current_flow']}

    last_message = state["messages"][-1].content
    chain = supervisor_prompt | structured_llm_router
    route = await chain.ainvoke({
        "context_block": state.get("context_block", "No hay resumen."),
        "messages": "\n".join([f"{type(m).__name__}: {m.content}" for m in state["messages"][-6:]]),
        "last_message": last_message,
    })
    print(f"🚦 Decisión del Supervisor: Ir a '{route.next}'")
    # Guardamos el flujo actual para saber a dónde volver después de una herramienta
    return {"next_agent": route.next, "current_flow": route.next}

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

**Regla de relevancia, errores y escalamiento:**
- Después de usar `knowledge_search`, responde únicamente si la información encontrada responde directamente a la pregunta del usuario (relevancia alta y explícita).
- Si hay un error técnico al usar una herramienta o no puedes completar la acción, informa brevemente el problema y ofrece hablar con un asesor. Si el usuario acepta, llama a la herramienta `escalate_to_human(reason=...)`.
- Si los resultados no responden claramente (o son tangenciales), indica que no encontraste información relevante sobre esa pregunta, ofrece reformular o, si el usuario lo desea, propone hablar con un asesor. Si el usuario acepta, llama a `escalate_to_human`.
"""
        ),
        MessagesPlaceholder(variable_name="messages"),
    ]
)
knowledge_agent_runnable = knowledge_agent_prompt | llm.bind_tools([knowledge_search, escalate_to_human])

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
    escalate_to_human,
    get_user_appointments,
    cancel_appointment,
]
appointment_agent_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            """Eres un agente experto en agendar citas. Tu objetivo es guiar al usuario a través del proceso de reserva.

**FASE ACTUAL (revisa `service_id` para saber en qué fase estás):**
- **FASE 1: Identificación (si `service_id` es NULO)**
  1. Si el último mensaje del usuario SOLO expresa intención genérica (p. ej., "quiero agendar"), primero PREGUNTA de forma clara: "¿Qué servicio te gustaría agendar?". NO uses herramientas todavía.
  2. Solo si el ÚLTIMO mensaje contiene palabras clave de un servicio (p. ej., "masaje", "limpieza facial"), usa `knowledge_search` para identificarlo y luego PIDE confirmación.
  3. Tras confirmación explícita del usuario, usa `update_service_in_state` para guardar el servicio.

- **FASE 2: Reserva (si `service_id` YA EXISTE)**
  1. Tu misión es completar la reserva. NO busques servicios.
  2. Sigue estrictamente: Preguntar fecha -> `check_availability` (con `service_id`, `organization_id`, `check_date_str`) -> si hay slots, pedir hora y usar `select_appointment_slot` -> finalmente `book_appointment`.
  3. Nunca digas "no hay horarios" sin haber llamado antes a `check_availability` y sin haber verificado que la lista retornada esté vacía.
  4. Si el usuario expresa fechas relativas ("hoy", "mañana", "la otra semana", formatos DD/MM o DD-MM), primero usa `resolve_relative_date` con `timezone="America/Bogota"` para obtener `selected_date` en formato YYYY-MM-DD y luego llama a `check_availability`.
  5. Al llamar `select_appointment_slot` DEBES pasar el parámetro `available_slots` exactamente como fue devuelto por `check_availability`. Si `available_slots` está vacío o no existe, primero vuelve a ejecutar `check_availability` y recién después llama a `select_appointment_slot`.

**Cambio de servicio (cuando ya hay `service_id`):**
- Si el usuario menciona explícitamente otro servicio distinto al actual (por nombre o sinónimos) o rechaza el actual, primero CONFIRMA: "¿Deseas cambiar al servicio ‘X’?".
- Si confirma, usa `knowledge_search` para verificar el servicio y luego `update_service_in_state` con el nuevo `service_id` y `service_name`.
- Tras cambiar de servicio, considera el contexto volátil limpiado y vuelve a pedir fecha; continúa con `check_availability` -> `select_appointment_slot` -> `book_appointment`.

**Estado Actual:**
- Servicio: {service_name} (ID: {service_id})
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

1) Si el usuario NO menciona fecha: PIDE la fecha de la cita a cancelar. Ofrece listar citas futuras si no la recuerda.
2) Si menciona fecha relativa (hoy/mañana/otra semana/DD/MM), usa la herramienta `resolve_relative_date` (America/Bogota) y guarda `selected_date`.
3) Si hay `selected_date` pero NO hora, intenta `find_appointment_for_cancellation(contact_id, selected_date)`:
   - 0 resultados: informa y ofrece listar próximas citas.
   - 1 resultado: PIDE confirmación para cancelar esa cita.
   - >1 resultado: PIDE la hora exacta para desambiguar.
4) Si hay fecha y hora, llama `find_appointment_for_cancellation` con ambos y, si hay una sola, llama `cancel_appointment` y confirma.
5) Si el usuario no recuerda fecha, usa `get_upcoming_user_appointments(contact_id)` para listar y pide que elija una.

Responde breve y en segunda persona. No inventes datos. Usa herramientas cuando corresponda.

Manejo de errores:
- Si alguna herramienta falla o devuelve un error, informa que no pudiste completar la cancelación en este momento y ofrece hablar con un asesor. Si el usuario acepta, llama a `escalate_to_human(reason=...)`.

**Estilo de comunicación:**
- Empático y claro, con 1–2 emojis sutiles (p. ej., 🗓️, ⚠️, ✅). No abuses.
- Propón opciones concretas para facilitar la elección.

**Resolución de contacto (multitenancy):**
- Si `contact_id` es NULO, primero llama a `resolve_contact_on_booking(organization_id={organization_id}, phone_number={phone_number}, country_code={country_code})` para obtener/crear el contacto en CRM y usar su `contact_id` en las demás herramientas.
- Si `contact_id` YA existe, NO llames `resolve_contact_on_booking` ni indiques que vas a verificar el contacto.
- Si falta nombre/apellido, pídelos y vuelve a llamar pasando `first_name` y `last_name`. Luego, enlaza el contacto al hilo con `link_chat_identity_to_contact(chat_identity_id={chat_identity_id}, organization_id={organization_id}, contact_id=<id>)`.
"""),
        MessagesPlaceholder("messages")
    ])
    tools_for_cancel = [resolve_relative_date, find_appointment_for_cancellation, get_upcoming_user_appointments, cancel_appointment, escalate_to_human]
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

1) Si el usuario no da fecha/hora, pide fecha. Si dice relativa, usa `resolve_relative_date` (America/Bogota) y guarda `selected_date`.
2) Con `selected_date` (y hora si la da), usa `find_appointment_for_update(contact_id, date[, time])`.
   - 0 resultados: informa que no encontraste citas para esa fecha y ofrece listar próximas con `get_upcoming_user_appointments(contact_id)`.
   - 1 resultado: confirma explícitamente si desea confirmar esa cita y llama `confirm_appointment(appointment_id)`.
   - >1 resultado: pide la hora exacta para desambiguar.
3) Tras confirmar, responde con un mensaje de confirmación con fecha y hora.

Responde breve y con 1 emoji sutil.

Manejo de errores:
- Si alguna herramienta falla o devuelve un error, informa que no pudiste completar la confirmación y ofrece hablar con un asesor. Si el usuario acepta, llama a `escalate_to_human(reason=...)`.

**Resolución de contacto (multitenancy):**
- Si `contact_id` es NULO, primero llama a `resolve_contact_on_booking(organization_id={organization_id}, phone_number={phone_number}, country_code={country_code})` para obtener/crear el contacto en CRM y usar su `contact_id` en las demás herramientas.
- Si `contact_id` YA existe, NO llames `resolve_contact_on_booking` ni indiques que vas a verificar el contacto.
- Si la herramienta indica que faltan nombres, pide al usuario nombre y apellido, y vuelve a llamarla pasando `first_name` y `last_name`. Luego, enlaza el contacto al hilo con `link_chat_identity_to_contact(chat_identity_id={chat_identity_id}, organization_id={organization_id}, contact_id=<id>)`.
"""),
        MessagesPlaceholder("messages")
    ])
    tools_for_confirm = [resolve_relative_date, find_appointment_for_update, get_upcoming_user_appointments, confirm_appointment, escalate_to_human]
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
    prompt = ChatPromptTemplate.from_messages([
        ("system", """
Eres un asistente para reagendar citas. Flujo:

1) Si el usuario no da fecha/hora nueva, pide la nueva fecha. Si es relativa, usa `resolve_relative_date` (America/Bogota) y guarda `selected_date`.
2) Si el usuario conoce fecha/hora de la cita original: identifica la cita con `find_appointment_for_update(contact_id, old_date[, old_time])`. Si no la recuerda, ofrece listar con `get_upcoming_user_appointments(contact_id)`.
3) Con la cita identificada, usa su `service_id` para calcular disponibilidad en la nueva fecha: `check_availability(service_id, organization_id, selected_date)` y muestra opciones. Tras elección, usa `select_appointment_slot`.
4) Llama `reschedule_appointment(appointment_id, new_date, new_start_time, member_id, comment?)` y confirma el cambio.

Sé claro y breve, con 1 emoji.

Manejo de errores:
- Si alguna herramienta falla o devuelve un error, informa que no pudiste completar el reagendamiento y ofrece hablar con un asesor. Si el usuario acepta, llama a `escalate_to_human(reason=...)`.

**Resolución de contacto (multitenancy):**
- Si `contact_id` es NULO, primero llama a `resolve_contact_on_booking(organization_id={organization_id}, phone_number={phone_number}, country_code={country_code})` para obtener/crear el contacto en CRM y usar su `contact_id` en las demás herramientas.
- Si `contact_id` YA existe, NO llames `resolve_contact_on_booking` ni indiques que vas a verificar el contacto.
- Si falta nombre/apellido, pídelos y vuelve a llamar pasando `first_name` y `last_name`. Luego, enlaza el contacto al hilo con `link_chat_identity_to_contact(chat_identity_id={chat_identity_id}, organization_id={organization_id}, contact_id=<id>)`.
"""),
        MessagesPlaceholder("messages")
    ])
    tools_for_res = [resolve_relative_date, find_appointment_for_update, get_upcoming_user_appointments, check_availability, select_appointment_slot, reschedule_appointment, escalate_to_human]
    if not state.get("contact_id"):
        tools_for_res = [resolve_contact_on_booking, link_chat_identity_to_contact] + tools_for_res
    runnable = prompt | llm.bind_tools(tools_for_res)
    if os.getenv("LOG_VERBOSE", "false").lower() in ("1", "true", "yes"):
        print("[reschedule] Últimos mensajes:")
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
            print(f"🧰 (reschedule) tool_calls: {json.dumps(calls_summary, ensure_ascii=False)}")
        else:
            print(f"🗣️ (reschedule) respuesta directa: {getattr(response, 'content', '')[:300]}")
    except Exception:
        pass
    return {"messages": [response]}

async def escalation_node(state: GlobalState) -> Dict[str, Any]:
    print("--- 🔴 NODO: Escalamiento ---")
    return {"messages": [AIMessage(content="He notificado a un asesor para que se ponga en contacto contigo. En breve te contactaremos 🤝🙂")]}


# --- 4. Construcción del Grafo ---
workflow = StateGraph(GlobalState)

workflow.add_node("supervisor", supervisor_node)
workflow.add_node("knowledge", knowledge_node)
workflow.add_node("appointment", appointment_node)
workflow.add_node("cancellation", cancellation_node)
workflow.add_node("confirmation", confirmation_node)
workflow.add_node("reschedule", reschedule_node)
workflow.add_node("escalation", escalation_node)
tool_node = ToolNode(all_tools)
workflow.add_node("tools", tool_node)

async def apply_tool_effects(state: GlobalState) -> Dict[str, Any]:
    """Aplica efectos en el estado a partir del último ToolMessage si es estructurado."""
    print("--- 🔧 NODO: Aplicar efectos de herramientas ---")
    if not state["messages"]:
        return {}
    last_msg = state["messages"][-1]
    if not isinstance(last_msg, ToolMessage):
        return {}

    try:
        payload = json.loads(last_msg.content)
    except Exception:
        # Algunas tools devuelven texto plano; no mutamos estado
        return {}

    # Log básico del ToolMessage
    try:
        print(json.dumps({
            "tool_name": getattr(last_msg, "name", None),
            "raw_payload": payload if not isinstance(payload, list) else f"list[{len(payload)}]"
        }, ensure_ascii=False))
    except Exception:
        pass

    updates: Dict[str, Any] = {}
    tool_name = getattr(last_msg, "name", None)

    # Manejo específico por herramienta
    if tool_name == "update_service_in_state" and isinstance(payload, dict):
        if payload.get("action") == "update_service":
            updates["service_id"] = payload.get("service_id")
            updates["service_name"] = payload.get("service_name")
            # Al cambiar servicio, limpiar contexto volátil
            updates["available_slots"] = None
            updates["selected_date"] = None
            updates["selected_time"] = None
            updates["selected_member_id"] = None
            updates["ready_to_book"] = None
    elif tool_name == "resolve_relative_date" and isinstance(payload, dict):
        if payload.get("selected_date"):
            updates["selected_date"] = payload.get("selected_date")
    elif tool_name == "resolve_contact_on_booking" and isinstance(payload, dict):
        # Si resolvió/creó contacto, mantenerlo en estado para pasos siguientes
        if payload.get("success") and payload.get("contact_id"):
            updates["contact_id"] = payload.get("contact_id")
    elif tool_name == "check_availability" and isinstance(payload, list):
        # Guardar slots disponibles completos en estado
        updates["available_slots"] = payload
        print(f"📦 available_slots actualizados: {len(payload)}")
    elif tool_name == "select_appointment_slot" and isinstance(payload, dict):
        if payload.get("success") is False:
            # No se encontró el slot; forzar recálculo de disponibilidad en el siguiente turno
            updates["available_slots"] = None
        else:
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

workflow.add_node("apply_tool_effects", apply_tool_effects)

workflow.set_entry_point("supervisor")

workflow.add_conditional_edges(
    "supervisor",
    lambda state: state.get("next_agent"),
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

workflow.add_edge("tools", "apply_tool_effects")
workflow.add_edge("apply_tool_effects", "supervisor")

# --- 5. Compilación y FastAPI ---
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
        initial_state = GlobalState(
            messages=conversation_history,
            organization_id=payload.organizationId,
            chat_identity_id=payload.chatIdentityId,
            contact_id=payload.contactId,
            phone=payload.phone,
            phone_number=payload.phoneNumber,
            country_code=payload.countryCode,
            context_block=context_block,
        )
        
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
                "available_slots_len": len(final_state_result.get("available_slots") or [])
            }, ensure_ascii=False))
        except Exception:
            pass

        # Intentar actualizar el resumen del hilo de forma asíncrona (best-effort)
        try:
            last_msgs = await sb_get_last_messages(session_id, last_n=12)
            await _maybe_update_thread_summary(payload.organizationId, session_id, last_msgs)
        except Exception:
            pass
        return {"response": ai_response_content}

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": "Internal server error."}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
