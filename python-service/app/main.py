import uvicorn
from fastapi import FastAPI, Request
from pydantic import BaseModel
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import HumanMessage, AIMessage, BaseMessage, ToolMessage
from typing import Optional, Dict, Any, Literal
import json

# 1. Importaciones de la nueva arquitectura
from .state import GlobalState
from .tools import (
    all_tools, knowledge_search, check_availability, 
    select_appointment_slot, book_appointment,
    update_service_in_state, 
    escalate_to_human, get_user_appointments, cancel_appointment
)
from langgraph.prebuilt import ToolNode

# Zep Cloud Imports
from .zep import add_messages_to_zep, ensure_user_exists, ensure_thread_exists, get_zep_context_block, get_zep_last_messages
from .db import supabase_client
from langchain_openai import ChatOpenAI
import os
from pydantic import BaseModel as PydanticBaseModel
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

# --- 2. Supervisor y Enrutador ---

class Route(PydanticBaseModel):
    """Decide a qué nodo dirigir la conversación a continuación."""
    next: Literal[
        "knowledge", "appointment", "cancellation", "confirmation",
        "escalation", "__end__"
    ]

# Permite configurar el modelo por variable de entorno (p. ej., OPENAI_CHAT_MODEL=gpt-4.1-nano)
model_name = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o")
llm = ChatOpenAI(model=model_name, temperature=0)
print(f"⚙️ Modelo OpenAI activo: {model_name}")
structured_llm_router = llm.with_structured_output(Route)

supervisor_prompt = ChatPromptTemplate.from_messages(
    [
        ("system", """
Eres el supervisor de un sistema de agentes de IA para un centro de estética. Tu única tarea es enrutar la conversación al nodo correcto.

**Nodos Disponibles:**
- `knowledge`: Para saludos iniciales y preguntas generales sobre la empresa o servicios. Es puramente informativo.
- `appointment`: Cuando el usuario expresa explícitamente el deseo de agendar, reservar o pedir una cita.
- `cancellation`: Si el usuario quiere cancelar o reprogramar una cita existente.
- `escalation`: Si el usuario está frustrado, pide hablar con un humano o el sistema falla repetidamente.
- `__end__`: Únicamente para despedidas explícitas (ej. "adiós", "chao") o cuando la conversación ha concluido.

**Contexto de Zep (Memoria a Largo Plazo):**
{zep_context}
**Últimos mensajes (Memoria a Corto Plazo):**
{messages}
Basado en el **último mensaje del usuario** y el contexto de Zep, decide el siguiente nodo."""),
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
        "zep_context": state.get("zep_context", "No hay resumen."),
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

**Contexto de Zep:** {zep_context}

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
- Mantén un tono empático y humano.

**Regla de relevancia y escalamiento:**
- Después de usar `knowledge_search`, responde únicamente si la información encontrada responde directamente a la pregunta del usuario (relevancia alta y explícita).
- Si los resultados no responden claramente (o son tangenciales), indica que no encontraste información relevante sobre esa pregunta, ofrece reformular o, si el usuario lo desea, propone hablar con un asesor humano. Si el usuario acepta, dirígete al nodo de `escalation`.
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
            "zep_context": state.get("zep_context", "No hay resumen."),
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
)
appointment_tools = [
    knowledge_search,
    update_service_in_state,
    resolve_relative_date,
    check_availability,
    select_appointment_slot,
    book_appointment,
    resolve_contact_on_booking,
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
  1. Si el último mensaje del usuario SOLO expresa intención genérica (p. ej., "quiero agendar"), primero PREGUNTA de forma clara: "¿Qué servicio te gustaría agendar?" y da 2-3 ejemplos. NO uses herramientas todavía.
  2. Solo si el ÚLTIMO mensaje contiene palabras clave de un servicio (p. ej., "masaje", "limpieza facial"), usa `knowledge_search` para identificarlo y luego PIDE confirmación.
  3. Tras confirmación explícita del usuario, usa `update_service_in_state` para guardar el servicio.

- **FASE 2: Reserva (si `service_id` YA EXISTE)**
  1. Tu misión es completar la reserva. NO busques servicios.
  2. Sigue estrictamente: Preguntar fecha -> `check_availability` (con `service_id`, `organization_id`, `check_date_str`) -> si hay slots, pedir hora y usar `select_appointment_slot` -> finalmente `book_appointment`.
  3. Nunca digas "no hay horarios" sin haber llamado antes a `check_availability` y sin haber verificado que la lista retornada esté vacía.
  4. Si el usuario expresa fechas relativas ("hoy", "mañana", "la otra semana", formatos DD/MM o DD-MM), primero usa `resolve_relative_date` con `timezone="America/Bogota"` para obtener `selected_date` en formato YYYY-MM-DD y luego llama a `check_availability`.

**Cambio de servicio (cuando ya hay `service_id`):**
- Si el usuario menciona explícitamente otro servicio distinto al actual (por nombre o sinónimos) o rechaza el actual, primero CONFIRMA: "¿Deseas cambiar al servicio ‘X’?".
- Si confirma, usa `knowledge_search` para verificar el servicio y luego `update_service_in_state` con el nuevo `service_id` y `service_name`.
- Tras cambiar de servicio, considera el contexto volátil limpiado y vuelve a pedir fecha; continúa con `check_availability` -> `select_appointment_slot` -> `book_appointment`.

**Estado Actual:**
- Servicio: {service_name} (ID: {service_id})
- Fecha: {selected_date}
- Hora: {selected_time}
**Contexto Zep:** {zep_context}

**Variables para herramientas (multitenancy):**
- organization_id: {organization_id}
- contact_id: {contact_id}
- phone: {phone}
- phone_number: {phone_number}
- country_code: {country_code}

Al invocar herramientas, usa estos valores exactamente para los parámetros correspondientes.

Si `contact_id` es nulo y necesitas agendar, primero llama a `resolve_contact_on_booking` con `organization_id`, `phone_number` y `country_code` para obtener/crear el contacto.

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
            "zep_context": state.get("zep_context", "No hay resumen."),
            "messages": state["messages"],
            "organization_id": state.get("organization_id"),
            "contact_id": state.get("contact_id"),
            "phone": state.get("phone"),
            "phone_number": state.get("phone_number"),
            "country_code": state.get("country_code"),
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

**Estilo de comunicación:**
- Empático y claro, con 1–2 emojis sutiles (p. ej., 🗓️, ⚠️, ✅). No abuses.
- Propón opciones concretas para facilitar la elección.
"""),
        MessagesPlaceholder("messages")
    ])
    tools_for_cancel = [resolve_relative_date, find_appointment_for_cancellation, get_upcoming_user_appointments, cancel_appointment]
    runnable = prompt | llm.bind_tools(tools_for_cancel)
    response = await runnable.ainvoke({"messages": state["messages"]})
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
workflow.add_node("confirmation", confirmation_node := cancellation_node)
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
    elif tool_name == "check_availability" and isinstance(payload, list):
        # Guardar slots disponibles completos en estado
        updates["available_slots"] = payload
        print(f"📦 available_slots actualizados: {len(payload)}")
    elif tool_name == "select_appointment_slot" and isinstance(payload, dict):
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
        "escalation": "escalation",
        "__end__": END
    }
)

workflow.add_conditional_edges("knowledge", decide_after_agent)
workflow.add_conditional_edges("appointment", decide_after_agent)
workflow.add_conditional_edges("cancellation", decide_after_agent)
workflow.add_conditional_edges("escalation", decide_after_agent)

workflow.add_edge("tools", "apply_tool_effects")
workflow.add_edge("apply_tool_effects", "supervisor")

# --- 5. Compilación y FastAPI ---
memory_saver = MemorySaver()
app_graph = workflow.compile(checkpointer=memory_saver)

class InvokePayload(BaseModel):
    organizationId: str
    chatIdentityId: str
    contactId: Optional[str]
    phone: str
    phoneNumber: str
    countryCode: str
    message: str

app = FastAPI()

async def get_contact_data(contact_id: str, organization_id: str) -> Optional[Dict[str, Any]]:
    try:
        response = await supabase_client.table('contacts').select('first_name, last_name').eq('id', contact_id).eq('organization_id', organization_id).maybe_single().execute()
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
        first_name, last_name = "Usuario", ""
        if payload.contactId:
            contact_data = await get_contact_data(payload.contactId, payload.organizationId)
            if contact_data:
                first_name = contact_data.get("first_name", "Usuario")
                last_name = contact_data.get("last_name", "")

        await ensure_user_exists(user_id, first_name, last_name, "")
        await ensure_thread_exists(session_id, user_id)
        
        from zep_cloud import Message
        await add_messages_to_zep(session_id, [Message(role="user", content=payload.message)])

        zep_context = await get_zep_context_block(session_id, mode="basic")
        zep_history = await get_zep_last_messages(session_id, last_n=3)
        
        # Compatibilidad v2→v3: role_type→role, role→name
        def _to_message(msg):
            role = getattr(msg, 'role', None) or getattr(msg, 'role_type', None)
            content = getattr(msg, 'content', '')
            if role in ('assistant', 'ai'):
                return AIMessage(content=content)
            return HumanMessage(content=content)

        conversation_history = [_to_message(msg) for msg in zep_history]
        conversation_history.append(HumanMessage(content=payload.message))

        config = {"configurable": {"thread_id": session_id}}
        initial_state = GlobalState(
            messages=conversation_history,
            organization_id=payload.organizationId,
            chat_identity_id=payload.chatIdentityId,
            contact_id=payload.contactId,
            phone=payload.phone,
            phone_number=payload.phoneNumber,
            country_code=payload.countryCode,
            zep_context=zep_context,
        )
        
        final_state_result = await app_graph.ainvoke(initial_state, {**config, "recursion_limit": 50})

        ai_response_content = "No pude procesar tu solicitud."
        if final_state_result and final_state_result.get("messages"):
            last_message = final_state_result["messages"][-1]
            if isinstance(last_message, AIMessage) and not last_message.tool_calls:
                ai_response_content = last_message.content

        await add_messages_to_zep(session_id, [Message(role="assistant", content=ai_response_content)])
        return {"response": ai_response_content}

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": "Internal server error."}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
