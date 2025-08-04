from typing import Literal, Union
from openai import OpenAI
import pydantic_ai
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from langgraph.types import Command
from langgraph.graph import END
from langchain_core.messages import AIMessage, HumanMessage, trim_messages

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
async def supervisor_node(state: GlobalState) -> Command[Literal[*AGENT_NAMES, "__end__"]]:
    """
    Este nodo orquesta el flujo de trabajo. Analiza el mensaje más reciente del usuario
    y decide qué hacer a continuación, utilizando el historial como contexto.
    """
    print("--- Supervisor ---")
    
    # DEBUG: Mostrar el estado que recibe el supervisor
    print(f"🔍 DEBUG Supervisor recibiendo estado:")
    print(f"🔍 service_id: {state.get('service_id')}")
    print(f"🔍 service_name: {state.get('service_name')}")
    print(f"🔍 organization_id: {state.get('organization_id')}")

    # --- 1. Extraer el último mensaje del usuario Y verificar si hay respuesta de agente ---
    latest_user_message = next((m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None)
    latest_ai_message = next((m for m in reversed(state["messages"]) if isinstance(m, AIMessage)), None)

    if not latest_user_message:
        print("⚠️ No se encontró un mensaje de usuario para procesar. Terminando.")
        return Command(goto="__end__")
    
    # --- DETECTAR SI VENIMOS DE UN AGENTE QUE YA ACTUALIZÓ EL ESTADO ---
    # Si el último mensaje AI es más reciente que el último mensaje del usuario,
    # significa que un agente ya respondió y debemos considerar el contexto actualizado
    if (latest_ai_message and 
        latest_user_message and 
        state["messages"].index(latest_ai_message) > state["messages"].index(latest_user_message)):
        
        print("🔄 DETECTADO: Regresando de un agente que ya procesó la solicitud")
        print(f"🔄 Último mensaje AI: {latest_ai_message.name} - {latest_ai_message.content[:100]}...")
        
        # Si el KnowledgeAgent ya resolvió un servicio y hay service_id, ir directo a AppointmentAgent
        if (latest_ai_message.name == "KnowledgeAgent" and 
            state.get('service_id') and 
            any(keyword in latest_user_message.content.lower() for keyword in ["agendar", "reservar", "programar", "cita"])):
            
            print("🔄 KnowledgeAgent resolvió servicio + usuario quiere reservar → AppointmentAgent")
            return Command(goto="AppointmentAgent")
        
        # Si cualquier agente ya terminó su tarea, terminar
        elif latest_ai_message.name in ["KnowledgeAgent", "AppointmentAgent", "EscalationAgent"]:
            print("🔄 Agente ya completó la tarea → Terminando")
            return Command(goto="__end__")

    # --- 2. Preparar el contexto para el LLM ---
    history_messages = state["messages"][:-1]
    history_str = "\n".join([f"{msg.__class__.__name__}: {msg.content}" for msg in history_messages])
    
    zep_context = ""
    if state.get("chat_identity_id"):
        thread_id = state['chat_identity_id']
        try:
            zep_context = await get_zep_memory_context(thread_id)
            if zep_context:
                print(f"🧠 Contexto Zep para thread {thread_id} encontrado.")
        except Exception as e:
            print(f"⚠️ No se pudo obtener contexto de Zep para thread {thread_id}. Error: {e}")

    # --- 3. Construir el Prompt para el Supervisor ---
    system_prompt = f"""Eres un asistente virtual experto de la empresa. Tu única función es analizar el MENSAJE MÁS RECIENTE del usuario y decidir el siguiente paso.

    **Contexto de la Conversación (Mensajes Anteriores):**
    {history_str}

    **Memoria a Largo Plazo (Datos del Cliente):**
    {zep_context}

    **INSTRUCCIONES CRÍTICAS:**
    1.  Tu foco principal es el **"MENSAJE DEL USUARIO A PROCESAR"**.
    2.  Usa el contexto y la memoria SÓLO para entender la intención del mensaje actual.
    3.  **NO respondas a mensajes antiguos.** Tu tarea es actuar sobre el último input.
    4.  Sé decisivo y claro en tu enrutamiento.

    **Estado Actual:**
    - Service ID en estado: {state.get('service_id')}
    - Service Name en estado: {state.get('service_name')}

    **Reglas de Enrutamiento:**
    -   Para saludos, despedidas o charla casual: responde directamente y termina (`terminate`).
    -   Para consultas **VAGAS** (ej: "info", "ayuda"): responde pidiendo más detalles y termina (`terminate`).
    -   Para preguntas **ESPECÍFICAS** sobre servicios, precios, ubicación, horarios: enruta a `KnowledgeAgent`.
    -   Si un usuario quiere **reservar/agendar/programar** algo:
        -   Si NO hay service_id en estado O el usuario menciona un servicio específico diferente al actual: enruta a `KnowledgeAgent` PRIMERO.
        -   Si YA hay service_id y el usuario solo quiere continuar con la reserva: enruta a `AppointmentAgent`.
    -   Si el usuario pide explícitamente hablar con un **humano/asesor**: enruta a `EscalationAgent`.
    -   Si el mensaje del usuario es una simple confirmación (ej: "ok", "listo") y la tarea anterior ya se completó: responde amablemente y termina (`terminate`).
    """
    
    user_input_for_llm = f"""
    **MENSAJE DEL USUARIO A PROCESAR:**
    "{latest_user_message.content}"
    """

    # --- 4. Invocar el LLM (Supervisor) ---
    supervisor_agent = Agent[GlobalState](
        'openai:gpt-4o',
        deps_type=GlobalState,
        result_type=SupervisorOutput,
        system_prompt=system_prompt
    )
    
    print(f"🔍 Invocando supervisor para el mensaje: '{latest_user_message.content}'")
    result = await supervisor_agent.run(user_input_for_llm, deps=state)
    next_agent_value = result.data.next_agent
    direct_response = result.data.direct_response
    
    print(f"✅ Supervisor decidió enrutar a: {next_agent_value}")

    # --- 5. Retornar el Comando ---
    if direct_response:
        print(f"📝 Supervisor generando respuesta directa: '{direct_response[:100]}...'")
        ai_message = AIMessage(content=direct_response, name="Supervisor")
        
        # Obtenemos los mensajes actuales para añadir el nuevo
        current_messages = state.get("messages", [])
        
        return Command(
            update={"messages": current_messages + [ai_message]},
            goto="__end__"
        )
    
    # Si no hay respuesta directa, simplemente enruta al siguiente agente.
    # El agente se encargará de añadir su propia respuesta al estado.
    # IMPORTANTE: Mantener el estado actual al enrutar
    
    print(f"🔍 DEBUG Supervisor enviando a {next_agent_value} con estado:")
    print(f"🔍 service_id: {state.get('service_id')}")
    print(f"🔍 service_name: {state.get('service_name')}")
    
    return Command(goto=next_agent_value)
