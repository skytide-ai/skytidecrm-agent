from typing import Literal
import os
from openai import OpenAI
import pydantic_ai
from pydantic import BaseModel, Field

# Importamos el estado global
from .state import GlobalState

# --- Cliente Pydantic AI con OpenRouter ---
# Creamos un cliente de OpenAI configurado para OpenRouter y lo parcheamos con pydantic_ai
# Esto nos permite usar el parámetro `response_model` en las llamadas al LLM
openai_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY"),
    default_headers={
        "HTTP-Referer": "https://skytidecrm.com",  # Tu sitio web
        "X-Title": "SkytideCRM Agent",  # Nombre de tu app
    }
)
client = pydantic_ai.patch(openai_client)

# 1. Definimos los nombres de nuestros agentes
AGENT_NAMES = ("KnowledgeAgent", "AppointmentAgent", "EscalationAgent")
TERMINATE = "terminate"

# 2. Definimos el esquema de salida estructurada para el supervisor
class SupervisorOutput(BaseModel):
    next_agent: Literal[*AGENT_NAMES, TERMINATE] = Field(
        description="El nombre del siguiente agente que debe actuar o 'terminate' si la tarea ha concluido."
    )

# 3. Creamos la función del nodo supervisor que se usará en el grafo
def supervisor_node(state: GlobalState) -> dict:
    """
    Este es el nodo que orquesta el flujo de trabajo. Llama al supervisor
    para decidir el siguiente paso usando pydantic-ai.
    """
    print("--- Supervisor ---")
    
    messages_str = "\n".join([f"{msg.role}: {msg.content}" for msg in state["messages"]])
    
    # Construimos la lista de mensajes para la API de OpenAI
    prompt_messages = [
        {
            "role": "system",
            "content": """
    Eres un supervisor experto en un sistema de CRM conversacional.
    Tu única función es analizar el estado actual de la conversación y decidir cuál de los siguientes agentes especializados debe actuar a continuación.
    
    Agentes disponibles:
    - KnowledgeAgent: Utilízalo cuando el usuario haga preguntas sobre servicios, precios, información general, o quiera agendar algo. Este agente encontrará la información y el ID del servicio.
    - AppointmentAgent: Utilízalo una vez que el 'KnowledgeAgent' haya identificado un 'service_id' y el usuario haya expresado su intención de agendar. Este agente gestiona la disponibilidad y la creación de la cita.
    - EscalationAgent: Utilízalo cuando el usuario pida explícitamente hablar con un humano o si la conversación se vuelve demasiado compleja para los otros agentes.
    
    Analiza los mensajes y el estado, y devuelve el nombre del siguiente agente a ejecutar.
    
    Flujo típico:
    - Si el usuario pregunta por servicios, información o quiere agendar → 'KnowledgeAgent'
    - Si ya tenemos un 'service_id' en el estado y el usuario quiere ver horarios o confirmar una cita → 'AppointmentAgent'
    - Si el usuario responde a una pregunta de aclaración de servicios → 'KnowledgeAgent'
    - Si el usuario dice "hablar con un asesor", "ayuda humana" o algo similar → 'EscalationAgent'
    - Si el último mensaje es de la IA y no hay follow-up, la conversación probablemente ha terminado → 'terminate'
            """
        },
        {
            "role": "user",
            "content": messages_str
        }
    ]

    # Invocamos al cliente de OpenRouter parcheado con pydantic_ai
    result: SupervisorOutput = client.chat.completions.create(
        model="openai/gpt-4o",  # Modelo de OpenAI a través de OpenRouter
        response_model=SupervisorOutput,
        messages=prompt_messages
    )
    
    next_agent = result.next_agent
    print(f"Supervisor ha decidido enrutar a: {next_agent}")

    return {"next_agent": next_agent} 