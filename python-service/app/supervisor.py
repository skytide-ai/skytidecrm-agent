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
    Supervisor inteligente con análisis contextual avanzado.
    Usa context-aware decision making para routing inteligente.
    """
    print("--- Supervisor Inteligente ---")
    
    # --- 1. EXTRAER CONTEXTO COMPLETO ---
    latest_user_message = next((m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None)
    latest_ai_message = next((m for m in reversed(state["messages"]) if isinstance(m, AIMessage)), None)

    if not latest_user_message:
        print("⚠️ No se encontró un mensaje de usuario para procesar. Terminando.")
        return Command(goto="__end__")
    
    # --- 2. ANÁLISIS INTELIGENTE DE CONTEXTO ---
    current_service_id = state.get('service_id')
    current_service_name = state.get('service_name')
    user_query = latest_user_message.content
    
    print(f"🧠 CONTEXTO ACTUAL:")
    print(f"   - service_id: {current_service_id}")
    print(f"   - service_name: {current_service_name}")
    print(f"   - Usuario dice: '{user_query}'")
    
    # --- 3. DETECCIÓN DE FLUJO COMPLETADO ---
    # Si hay una respuesta AI más reciente que el mensaje del usuario, la conversación está completa
    if (latest_ai_message and 
        latest_user_message and 
        state["messages"].index(latest_ai_message) > state["messages"].index(latest_user_message)):
        
        print("✅ Conversación completada por un agente → Terminando")
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

    # --- 3. Construir el Prompt Inteligente para el Supervisor ---
    system_prompt = f"""🧠 SUPERVISOR INTELIGENTE CON ANÁLISIS CONTEXTUAL AVANZADO

**TU MISIÓN:** Analizar la intención REAL del usuario y tomar la decisión MÁS INTELIGENTE sobre el próximo paso.

**CONTEXTO ACTUAL:**
- Estado del servicio: {"🎯 SERVICIO IDENTIFICADO (" + str(current_service_name) + ")" if current_service_id else "❌ SIN SERVICIO"}
- Service ID: {current_service_id or "None"}

**HISTORIAL DE LA CONVERSACIÓN:**
{history_str}

**MEMORIA DEL USUARIO:**
{zep_context or "Sin información previa"}

**🎯 ANÁLISIS INTELIGENTE DE INTENCIONES:**

1. **INFORMACIÓN GENERAL/EXPLORATORIA**: 
   - "¿Qué servicios tienen?", "¿Qué ofrecen?", "Cuéntame sobre sus servicios"
   → `KnowledgeAgent` (NO guardar service_id, solo informar)

2. **INFORMACIÓN ESPECÍFICA DE UN SERVICIO**: 
   - "¿Cuánto cuesta la limpieza facial?", "¿En qué consiste el masaje?"
   → `KnowledgeAgent` (puede obtener service_id para contexto futuro, pero SIN compromiso de agendar)

3. **INTENCIÓN CLARA DE AGENDAR**:
   - "Quiero agendar...", "Me gustaría reservar...", "¿Puedo programar...?"
   - Si YA hay service_id: → `AppointmentAgent` (directo al agendamiento)
   - Si NO hay service_id: → `KnowledgeAgent` (identificar servicio primero, LUEGO agendar)

4. **CAMBIO DE TEMA/SERVICIO**: 
   - Si pregunta por OTRO servicio diferente al actual
   → `KnowledgeAgent` (buscar nuevo servicio, solo guardar ID si va a agendar)

5. **ESCALACIÓN**: 
   - "Quiero hablar con alguien", "¿Hay un asesor disponible?"
   → `EscalationAgent`

6. **CONVERSACIÓN CASUAL**: 
   - Saludos, despedidas, agradecimientos, confirmaciones simples
   → `terminate` (responde directamente y amablemente)

**REGLAS DE ORO**: 
- Service_id se guarda SOLO cuando hay intención CLARA de agendar
- Para consultas puramente informativas, NO es necesario guardar service_id
- Analiza la INTENCIÓN REAL del usuario, no solo palabras clave
- Un usuario puede preguntar sobre múltiples servicios sin querer agendar ninguno
- Solo cuando dice "quiero agendar X" es que necesita el service_id guardado"""
    
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
