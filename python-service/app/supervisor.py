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

    # 3. Lógica de Enrutamiento Basada en Estado Explícito (`booking_status`)
    # Este es el núcleo de la nueva arquitectura de flujos.
    
    booking_status = state.get("booking_status")
    print(f"🚥 SUPERVISOR: Estado de flujo actual -> {booking_status}")

    context_prompt = ""
    # --- FLUJO DE AGENDAMIENTO ---
    if booking_status == 'NEEDS_DATE':
        print("-> FLUJO: Necesita Fecha.")
        context_prompt = """
        **¡ATENCIÓN! ESTADO: `NEEDS_DATE`**
        - El usuario ha seleccionado un servicio. Tu ÚNICA misión es pedirle la fecha.
        - **Acción Obligatoria:** Enruta a `AppointmentAgent`.
        """
    elif booking_status == 'NEEDS_SLOT_SELECTION':
        print("-> FLUJO: Necesita Selección de Horario.")
        context_prompt = """
        **¡ATENCIÓN! ESTADO: `NEEDS_SLOT_SELECTION`**
        - Se le acaban de mostrar horarios al usuario.
        - **Acción Obligatoria:** Enruta a `AppointmentAgent` para que procese la selección del usuario.
        """
    elif booking_status == 'NEEDS_CONTACT_INFO':
        print("-> FLUJO: Necesita Información de Contacto.")
        context_prompt = """
        **¡ATENCIÓN! ESTADO: `NEEDS_CONTACT_INFO`**
        - El usuario ha seleccionado un horario.
        - **Acción Obligatoria:** Enruta a `AppointmentAgent` para que resuelva o pida los datos del contacto.
        """
    # --- FLUJO GENERAL (Cuando no hay un estado de agendamiento activo) ---
    else:
        print("-> FLUJO: General / Indeterminado.")
        # Revisa si el último mensaje del asistente fue la pregunta inicial
        latest_ai_message = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        if latest_ai_message and "¿Qué servicio te gustaría reservar?" in latest_ai_message.content:
            print("-> CONTEXTO ADICIONAL: Respondiendo a pregunta inicial.")
            context_prompt = """
            **¡ATENCIÓN! CONTEXTO: SELECCIÓN INICIAL DE SERVICIO**
            - Acabas de preguntar qué servicio desea el usuario. Su respuesta es el nombre del servicio.
            - **Acción Obligatoria:** Enruta a `KnowledgeAgent` para buscar este servicio.
            """
        else:
             context_prompt = """
            **CONTEXTO: GENERAL**
            - Analiza la intención del usuario.
            - Si quiere información o agendar -> `KnowledgeAgent` para empezar.
            - Si quiere hablar con un humano -> `EscalationAgent`.
            - Si es una conversación casual -> Responde directamente y termina (`__end__`).
            """
            
    routing_rules = """
    **Reglas Generales de Enrutamiento:**
    1.  Basa tu decisión PRIMERO en el estado de flujo (`booking_status`). Las instrucciones en el bloque `¡ATENCIÓN!` tienen prioridad absoluta.
    2.  Si no hay un estado de flujo, usa el contexto general para decidir.
    3.  Siempre devuelve un objeto `Router` completo.
    """
    
    system_prompt = f"{base_prompt}{context_prompt}{routing_rules}"
    
    supervisor_agent = Agent('openai:gpt-4o', output_type=Router, system_prompt=system_prompt)
    
    result = await supervisor_agent.run(latest_user_message.content, deps=state)
    router_output: Router = result.output

    # --- Lógica de Actualización de Estado Post-Agentes ---
    update_data = {}
    
    # Si venimos de KnowledgeAgent, transferimos la información al estado principal
    knowledge_result = state.get("knowledge_result")
    if knowledge_result:
        if knowledge_result.service_id:
            update_data["service_id"] = str(knowledge_result.service_id)
        if knowledge_result.service_name:
            update_data["service_name"] = knowledge_result.service_name
        
        # Actualizamos el booking_status si el KnowledgeAgent lo modificó
        if state.get("booking_status"):
            update_data["booking_status"] = state.get("booking_status")

        update_data["knowledge_result"] = None
        print(f"🧠 SUPERVISOR: Actualizando estado con datos de KnowledgeAgent y limpiando.")

    if router_output.next_agent == TERMINATE:
        ai_message = AIMessage(content=router_output.response or "¡Claro! ¿En qué más puedo ayudarte?", name="Supervisor")
        update_data["messages"] = state.get("messages", []) + [ai_message]
        return Command(update=update_data, goto=TERMINATE)

    return Command(update=update_data, goto=router_output.next_agent)

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
    Eres un Asistente de IA excepcional. Tu trabajo es tomar datos técnicos y transformarlos en una respuesta humana, cálida y útil. Tu objetivo principal es que el usuario sienta que está hablando con una persona amable, no con un robot.

    **REGLA DE ORO: ¡LEE EL CONTEXTO!**
    Antes de escribir una sola palabra, analiza TODO el contexto que se te proporciona:
    - **Pregunta Original del Usuario:** "{user_query}"
    - **Servicio en Foco:** {service_context}
    - **Información Técnica Encontrada:**
      ---
      {knowledge_result.raw_information}
      ---

    **TU MISIÓN - CÓMO CONSTRUIR LA RESPUESTA PERFECTA:**
    1.  **NO SEAS UN LORO:** Nunca, bajo ninguna circunstancia, te limites a copiar y pegar la "Información Técnica Encontrada". Tu trabajo es **interpretarla** y usarla para **responder directamente** a la "Pregunta Original del Usuario".
    2.  **SÉ CONVERSACIONAL:** Usa un tono amigable y natural. Utiliza emojis para darle calidez a la conversación 😊. Evita el formato markdown (como **negritas** o listas con guiones).
    3.  **SÉ CONCISO Y DIRECTO:** Responde solo lo que el usuario preguntó. No le des un resumen de toda la información si solo preguntó por el precio.
    4.  **UTILIZA EL NOMBRE DEL SERVICIO:** Si el "Servicio en Foco" está disponible, incorpóralo en tu respuesta para demostrar que tienes contexto.

    **EJEMPLOS DE LO QUE DEBES HACER (Y NO HACER):**

    - **CASO 1: El usuario pregunta por el precio de un servicio.**
      - **MALO (robótico):** "La información encontrada es: Precio: $90.000 COP."
      - **BUENO (humano):** "¡Claro! El precio de la Limpieza Facial Profunda es de $90.000 COP. ✨ ¿Te gustaría que busquemos una fecha para agendarla?"

    - **CASO 2: El usuario pregunta por las contraindicaciones.**
      - **MALO (demasiado técnico):** "Contraindicaciones: Acné activo severo, piel quemada por el sol."
      - **BUENO (humano):** "Una cosa importante a tener en cuenta para la Limpieza Facial Profunda es que no se recomienda si tienes acné muy activo o la piel quemada por el sol. ☀️"

    - **CASO 3: El usuario acaba de seleccionar un servicio y tú debes presentarlo.**
      - **MALO (volcado de datos):** "Limpieza profesional del rostro que incluye exfoliación, extracción de impurezas, mascarilla y masaje facial. Precio: $90.000 COP..."
      - **BUENO (conversacional y proactivo):** "¡Perfecto! La Limpieza Facial Profunda es genial para renovar la piel. Incluye exfoliación, extracción y mascarilla. 😊 ¿Te gustaría que te cuente más o buscamos directamente una fecha para tu cita?"

    Ahora, crea la respuesta ideal para la pregunta del usuario.
    """

    formatter_agent = Agent('openai:gpt-4o', system_prompt=prompt)
    result = await formatter_agent.run("") 
    
    formatted_response = str(result.output)
    ai_message = AIMessage(content=formatted_response, name="Formatter")
    
    update_data = {"messages": state["messages"] + [ai_message]}
    if knowledge_result.service_id:
        update_data["service_id"] = str(knowledge_result.service_id)
    if knowledge_result.service_name:
        update_data["service_name"] = knowledge_result.service_name
        
    return Command(update=update_data, goto=TERMINATE)
