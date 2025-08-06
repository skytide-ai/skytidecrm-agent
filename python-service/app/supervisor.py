from typing import Literal, Optional
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from langgraph.types import Command
from langchain_core.messages import AIMessage, HumanMessage
import json

from .state import GlobalState
from .agents.knowledge_agent import KnowledgeSearchResult

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

    # Construcción dinámica del system_prompt basado en el contexto
    base_prompt = f"""
    Eres un supervisor de agentes de IA. Tu misión es analizar la consulta del usuario y decidir el siguiente paso usando el modelo Router.
    Agentes Disponibles: {', '.join(AGENT_NAMES)}.
    """

    context_prompt = ""
    if state.get("available_slots"):
        print("-> Contexto de Agendamiento Detectado.")
        context_prompt = """
        **¡ATENCIÓN! CONTEXTO DE AGENDAMIENTO ACTIVO:**
        - Acabas de presentar una lista de horarios disponibles para un servicio. Analiza la respuesta del usuario con las siguientes prioridades:
        - **Prioridad 1 (Selección de Horario):** Si el usuario elige una de las opciones mostradas (ej: "la opción 2", "a las 10 am", "perfecto, esa"), enruta a `AppointmentAgent` para que pueda confirmar la cita.
        - **Prioridad 2 (Ajuste de Fecha/Hora):** Si el usuario pide una FECHA u HORA DIFERENTE para el MISMO servicio (ej: "¿y para el día 15?", "¿tienes algo por la tarde?"), enruta a `AppointmentAgent` para que busque nueva disponibilidad.
        - **Prioridad 3 (Cambio de Tema):** Si el usuario cambia completamente de tema y pregunta por un SERVICIO DIFERENTE o información general (ej: "mejor quiero un masaje", "¿cuánto costaba X?", "dime la ubicación"), DEBES enrutar a `KnowledgeAgent`.
        """

    routing_rules = """
    **Reglas Generales de Enrutamiento:**
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

    prompt = f"""
    Eres un asistente de IA amigable. Transforma la siguiente información técnica en una respuesta cálida y natural para un cliente.
    Usa emojis y un tono cercano.

    Información Técnica:
    ---
    {knowledge_result.raw_information}
    ---
    Crea una respuesta amigable basada en esa información.
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
