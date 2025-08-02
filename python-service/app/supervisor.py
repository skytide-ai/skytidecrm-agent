from typing import Literal
from openai import OpenAI
import pydantic_ai
from pydantic import BaseModel, Field
from pydantic_ai import Agent

# Importamos el estado global y funciones de Zep
from .state import GlobalState
from .zep import get_zep_memory_context

# --- Cliente Pydantic AI ---
# Creamos un cliente de OpenAI para el supervisor
client = OpenAI()

# 1. Definimos los nombres de nuestros agentes
AGENT_NAMES = ("KnowledgeAgent", "AppointmentAgent", "EscalationAgent")
TERMINATE = "terminate"

# 2. Definimos el esquema de salida estructurada para el supervisor
class SupervisorOutput(BaseModel):
    next_agent: Literal[*AGENT_NAMES, TERMINATE] = Field(
        description="El nombre del siguiente agente que debe actuar o 'terminate' si la tarea ha concluido."
    )
    direct_response: str | None = Field(
        default=None,
        description="Respuesta directa del supervisor cuando puede manejar la consulta (para saludos, cortesía, etc.). Solo incluir si next_agent es 'terminate' y quieres responder directamente."
    )

# 3. Creamos la función del nodo supervisor que se usará en el grafo
async def supervisor_node(state: GlobalState) -> dict:
    """
    Este es el nodo que orquesta el flujo de trabajo. Llama al supervisor
    para decidir el siguiente paso usando pydantic-ai.
    Ahora incluye contexto de memoria de Zep.
    """
    print("--- Supervisor ---")
    
    # Crear string de mensajes compatible con LangChain Messages
    messages_str = "\n".join([f"{msg.__class__.__name__}: {msg.content}" for msg in state["messages"]])
    
    # Obtener contexto de memoria de Zep si hay chat_identity_id
    zep_context = ""
    if state.get("chat_identity_id"):
        # Usamos chat_identity_id directamente como thread_id para consistencia
        thread_id = state['chat_identity_id']
        try:
            zep_context = await get_zep_memory_context(thread_id)
            if zep_context:
                 print(f"🧠 Contexto Zep para thread {thread_id} encontrado.")
        except Exception as e:
            # Es normal que al principio no haya contexto, no es un error crítico.
            print(f"⚠️ No se pudo obtener contexto de Zep para thread {thread_id} (puede ser nuevo). Error: {e}")
            zep_context = ""
    
    # Construir el system prompt enriquecido con contexto de Zep
    base_system_prompt = """
    Eres un asistente virtual amigable y profesional para un centro de belleza.
    
    Tu función principal es mantener una conversación natural y decidir cuándo es necesario usar agentes especializados.
    
    PUEDES RESPONDER DIRECTAMENTE A:
    - Saludos: "Hola", "Buenos días", "¿Cómo estás?"
    - Despedidas: "Adiós", "Gracias", "Hasta luego"
    - Agradecimientos: "Gracias", "Te agradezco"
    - Conversación general y cortesía
    
    USA AGENTES ESPECIALIZADOS PARA:
    - KnowledgeAgent: Preguntas específicas sobre servicios, precios, ubicación, horarios, contacto, o cuando quieren agendar.
    - AppointmentAgent: Una vez que KnowledgeAgent haya identificado un service_id y el usuario quiera agendar.
    - EscalationAgent: SOLO cuando pidan EXPLÍCITAMENTE hablar con un humano/asesor.
    
    INSTRUCCIONES:
    - Para saludos simples → responde directamente con naturalidad y pregunta cómo puedes ayudar → 'terminate'
    - Para preguntas específicas sobre servicios/info → 'KnowledgeAgent'
    - Para escalación explícita → 'EscalationAgent'
    - Si ya respondiste o un agente completó su tarea → 'terminate'
    
    Sé cálido, profesional y conversacional. Actúa como un verdadero asistente humano.
    """
    
    enhanced_system_prompt = base_system_prompt
    if zep_context:
        enhanced_system_prompt += f"\n\n--- CONTEXTO DE MEMORIA ZEP ---\n{zep_context}\n--- FIN CONTEXTO ---"
    
    # Construir el input para el agente
    messages_content = f"""
    Estado actual:
    - service_id: {state.get('service_id')}
    - contact_id: {state.get('contact_id')}
    - organization_id: {state.get('organization_id')}
    
    Conversación:
    {messages_str}
    """
    
    # 🔍 DEBUG: Log completo del input al supervisor
    print(f"🔍 DEBUG - System prompt:")
    print(enhanced_system_prompt[:500] + "..." if len(enhanced_system_prompt) > 500 else enhanced_system_prompt)
    print(f"🔍 DEBUG - Messages content:")
    print(messages_content)
    print(f"🔍 DEBUG - Zep context length: {len(zep_context) if zep_context else 0}")
    
    # Crear el agente supervisor inline
    supervisor_agent = Agent[GlobalState](
        'openai:gpt-4o',
        deps_type=GlobalState,
        result_type=SupervisorOutput,
        system_prompt=enhanced_system_prompt
    )
    
    # Ejecutar el agente - Pydantic AI usa el client internamente
    result = await supervisor_agent.run(messages_content, deps=state)
    next_agent_value = result.data.next_agent
    direct_response = result.data.direct_response
    
    print(f"Supervisor (con contexto Zep) ha decidido enrutar a: {next_agent_value}")
    if direct_response:
        print(f"📝 Supervisor respuesta directa: {direct_response[:100]}...")
    
    return_dict = {"next_agent": next_agent_value}
    if direct_response:
        return_dict["direct_response"] = direct_response
    
    return return_dict 