from typing import Literal, Optional
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from langgraph.types import Command
from langchain_core.messages import AIMessage, HumanMessage
import json

from .state import GlobalState
from .agents.knowledge_agent import KnowledgeSearchResult
from .zep import get_zep_memory_context

AGENT_NAMES = ("KnowledgeAgent", "AppointmentAgent", "EscalationAgent")
TERMINATE = "__end__"

class Router(BaseModel):
    """Define la ruta a seguir o la respuesta directa."""
    next_agent: Literal[*AGENT_NAMES, "Formatter", TERMINATE] = Field(description="El nombre del siguiente agente a invocar.")
    response: Optional[str] = Field(default=None, description="La respuesta conversacional si next_agent es '__end__'.")

async def supervisor_node(state: GlobalState) -> Command:
    """Nodo supervisor que enruta de forma estructurada."""
    print("--- Supervisor Estructurado ---")
    latest_user_message = next((m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None)
    if not latest_user_message:
        return Command(goto=TERMINATE)

    # 1. Obtener memoria de Zep (largo plazo)
    zep_context = ""
    if state.get("chat_identity_id"):
        try:
            zep_context = await get_zep_memory_context(state["chat_identity_id"], min_rating=0.0)
            print(f"🧠 Supervisor usando Contexto Zep: {zep_context}")
        except Exception as e:
            print(f"❌ Error obteniendo contexto de Zep en Supervisor: {e}")
            zep_context = ""
    
    # 2. Obtener historial de sesión (corto plazo), limitado a los últimos 10 mensajes
    messages = state.get("messages", [])
    history = "\n".join(
        [f"- {m.type}: {m.content}" for m in messages[-10:]]
    )

    # 3. Construcción del prompt para el supervisor
    base_prompt = f"""
    Eres un supervisor de IA experto en enrutamiento. Tu misión es analizar el contexto completo (memoria a largo plazo y conversación reciente) para decidir el siguiente paso.

    **MEMORIA A LARGO PLAZO (Zep) - Hechos y Resúmenes Clave:**
    {zep_context if zep_context else "Sin memoria a largo plazo."}
    
    **HISTORIAL DE CONVERSACIÓN RECIENTE:**
    {history}
    
    **ÚLTIMO MENSAJE DEL USUARIO:** "{latest_user_message.content}"
    
    Agentes Disponibles: {', '.join(AGENT_NAMES)}.
    """

    context_prompt = ""
    if state.get("available_slots"):
        print("-> CONTEXTO: Agendamiento (Slots Disponibles)")
        context_prompt = """
        **¡ATENCIÓN! CONTEXTO DE AGENDAMIENTO (FASE 1 - SELECCIÓN):**
        - Acabas de presentar horarios. La prioridad es manejar la selección del usuario.
        - **Prioridad 1 (Selección):** Si el usuario elige un horario (ej: "a las 10 am"), enruta a `AppointmentAgent`.
        - **Prioridad 2 (Ajuste):** Si pide OTRA FECHA/HORA (ej: "¿y mañana?"), enruta a `AppointmentAgent`.
        - **Prioridad 3 (Cambio de Tema):** Si pregunta por OTRO SERVICIO o tema, enruta a `KnowledgeAgent`.
        """
    elif state.get("service_id"):
        print("-> CONTEXTO: Agendamiento (Servicio Seleccionado)")
        context_prompt = f"""
        **¡ATENCIÓN! CONTEXTO DE AGENDAMIENTO (FASE 0 - RECOLECCIÓN DE FECHA):**
        - El usuario ya seleccionó o se le acaba de presentar el servicio '{state.get('service_name', 'desconocido')}' (ID: {state.get('service_id')}) y quiere agendar.
        - **Prioridad 0 (Confirmación):** Si el usuario simplemente confirma que quiere ese servicio (ej: "sí", "ese está bien", "perfecto"), enruta a `AppointmentAgent` para que pida la fecha.
        - **Prioridad 1 (Dar Fecha):** Si el usuario proporciona una fecha o referencia temporal (ej: "para mañana", "el lunes"), enruta a `AppointmentAgent` para que busque disponibilidad.
        - **Prioridad 2 (Pregunta sobre Servicio Actual):** Si el usuario pregunta algo más sobre el servicio actual (ej: "¿cuánto dura?", "y el precio?"), enruta a `KnowledgeAgent`. La búsqueda se filtrará automáticamente.
        - **Prioridad 3 (Cambio de Servicio):** Si pregunta por un servicio DIFERENTE, enruta a `KnowledgeAgent`.
        """
    else:
        print("-> CONTEXTO: General (Sin Selección)")

    routing_rules = """
    **Reglas Generales de Enrutamiento (si no hay contexto específico):**
    - **Consulta de Información:** Si el usuario pregunta por precios, horarios, servicios, etc., enruta a `KnowledgeAgent`.
    - **Intención de Agendar:** Si el usuario quiere reservar o agendar (y no hay un contexto de agendamiento activo), enruta a `AppointmentAgent`.
    - **Petición de Ayuda Humana:** Si el usuario está frustrado o pide hablar con una persona, enruta a `EscalationAgent`.
    - **Conversación Casual:** Si es un saludo, despedida o agradecimiento, responde amablemente y pregunta cómo puedes ayudar, y enruta a `__end__`.
    
    Siempre debes devolver un objeto `Router` completo.
    """
    
    system_prompt = f"{base_prompt}{context_prompt}{routing_rules}"
    
    supervisor_agent = Agent('openai:gpt-4o', output_type=Router, system_prompt=system_prompt)
    
    result = await supervisor_agent.run(latest_user_message.content, deps=state)
    router_output: Router = result.output

    if router_output.next_agent == TERMINATE:
        ai_message = AIMessage(content=router_output.response or "¡Claro! ¿En qué más puedo ayudarte?", name="Supervisor")
        return Command(update={"messages": state["messages"] + [ai_message]}, goto=TERMINATE)
    
    return Command(goto=router_output.next_agent)

async def response_formatter_node(state: GlobalState) -> Command:
    """Nodo que formatea la salida de datos crudos en una respuesta conversacional."""
    print("--- Formateador de Respuestas (Con Logs) ---")
    
    raw_result = state.get("knowledge_result")
    
    print("\n--- DEBUG Formatter ---")
    print(f"Tipo de `knowledge_result` recibido: {type(raw_result)}")
    print(f"Contenido de `knowledge_result`:\n{json.dumps(raw_result, indent=2) if isinstance(raw_result, dict) else raw_result}")
    
    knowledge_result = None
    if isinstance(raw_result, dict):
        knowledge_result = KnowledgeSearchResult(**raw_result)
    elif isinstance(raw_result, KnowledgeSearchResult):
        knowledge_result = raw_result

    # Ahora la lógica de validación es más robusta
    if not knowledge_result:
        print("Resultado: `knowledge_result` está vacío o no es válido.")
        response = "Lo siento, no pude procesar tu solicitud. ¿Prefieres hablar con un asesor?"
        ai_message = AIMessage(content=response, name="Formatter")
        return Command(update={"messages": state["messages"] + [ai_message]}, goto=TERMINATE)

    if knowledge_result.clarification_message:
        print(f"Resultado: Se encontró mensaje de clarificación: '{knowledge_result.clarification_message}'")
        response = f"Lo siento, tuve un problema. {knowledge_result.clarification_message} ¿Te gustaría que te conecte con un asesor?"
        ai_message = AIMessage(content=response, name="Formatter")
        return Command(update={"messages": state["messages"] + [ai_message]}, goto=TERMINATE)

    if not knowledge_result.raw_information:
        print("Resultado: No se encontró `raw_information` para formatear.")
        response = "Lo siento, no pude procesar tu solicitud. ¿Podrías intentarlo de nuevo o prefieres hablar con un asesor?"
        ai_message = AIMessage(content=response, name="Formatter")
        return Command(update={"messages": state["messages"] + [ai_message]}, goto=TERMINATE)
    
    print(f"Resultado: Se encontró `raw_information` de {len(knowledge_result.raw_information)} caracteres.")
    print("---------------------------\n")

    latest_user_message = next((m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None)
    user_query = latest_user_message.content if latest_user_message else ""

    service_context = f"Estamos hablando del servicio: '{knowledge_result.service_name}'." if knowledge_result.service_name else "No hay un servicio específico en contexto."

    prompt = f"""
    Eres un asistente de IA experto en crear respuestas naturales y contextualmente relevantes. Tu tarea es responder a la pregunta del usuario de forma precisa.

    **CONTEXTO DISPONIBLE:**
    - **Pregunta del Usuario:** "{user_query}"
    - **Contexto del Servicio:** {service_context}
    - **Información Encontrada:**
      ---
      {knowledge_result.raw_information}
      ---

    **TUS INSTRUCCIONES:**
    1.  Usa la "Información Encontrada" para responder a la "Pregunta del Usuario".
    2.  Si el "Contexto del Servicio" está disponible, ÚSALO para que tu respuesta suene más natural. Por ejemplo, en lugar de decir "Las contraindicaciones son...", di "Las contraindicaciones para el servicio de {knowledge_result.service_name} son...".
    3.  Sé directo, amigable y usa emojis 😊. No resumas toda la información, solo responde la pregunta.

    **Ejemplo de respuesta ideal:**
    "¡Claro! Las contraindicaciones para el servicio de Limpieza Facial Profunda son acné activo o tener la piel quemada por el sol ☀️."
    """

    formatter_agent = Agent('openai:gpt-4o', system_prompt=prompt)
    result = await formatter_agent.run("") 
    
    formatted_response = str(result.data)
    ai_message = AIMessage(content=formatted_response, name="Formatter")
    
    update_data = {"messages": state["messages"] + [ai_message]}
    if knowledge_result.service_id:
        update_data["service_id"] = str(knowledge_result.service_id)
    if knowledge_result.service_name:
        update_data["service_name"] = knowledge_result.service_name
        
    return Command(update=update_data, goto=TERMINATE)
