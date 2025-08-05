from typing import Literal
from pydantic_ai import Agent
from langgraph.types import Command
from langgraph.graph import END
from langchain_core.messages import AIMessage, HumanMessage, trim_messages

# Importamos el estado global y funciones de Zep
from .state import GlobalState
from .zep import get_zep_memory_context

# --- Cliente Pydantic AI ---
# 🧠 Supervisor usa Pydantic AI en lugar de cliente OpenAI directo

# 1. Definimos los nombres de nuestros agentes
AGENT_NAMES = ("KnowledgeAgent", "AppointmentAgent", "EscalationAgent")
TERMINATE = "terminate"

# 2. Definimos el esquema de salida estructurada para el supervisor
# 🧠 SUPERVISOR SEMÁNTICO: Sin modelos estructurados, usa comprensión natural del LLM

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

**🧠 ANÁLISIS SEMÁNTICO INTELIGENTE - SIN KEYWORDS**

TU TAREA: Analiza la INTENCIÓN REAL del usuario usando comprensión semántica avanzada, NO patrones de palabras específicas.

**TIPOS DE INTENCIÓN:**

1. **CONVERSACIÓN SOCIAL**: Saludos, despedidas, cortesía, preguntas personales sobre el asistente
   → `terminate` (responde amablemente)

2. **INFORMACIÓN DEL NEGOCIO**: Cualquier pregunta sobre servicios, productos, precios, ubicación, horarios, contacto
   → `KnowledgeAgent` (buscar información real)

3. **INTENCIÓN DE AGENDAR**: Usuario quiere reservar/programar algo
   - Si YA hay service_id: → `AppointmentAgent`
   - Si NO hay service_id: → `KnowledgeAgent` (identificar servicio primero)

4. **ESCALACIÓN HUMANA**: Usuario quiere hablar con una persona real
   → `EscalationAgent`

**🧠 ANÁLISIS SEMÁNTICO AVANZADO:**
- Entiende SINÓNIMOS y VARIACIONES naturales del lenguaje
- Analiza CONTEXTO y INTENCIÓN, no solo palabras exactas
- Distingue entre preguntas sobre TI (negocio) vs MÍ (asistente personal)
- Reconoce diferentes formas de expresar la misma intención

**EJEMPLOS DE COMPRENSIÓN SEMÁNTICA:**
- "¿Dónde quedan?" = "¿Cuál es la dirección?" = "¿Ubicación?" → INFORMACIÓN DEL NEGOCIO
- "Quiero cita" = "Necesito reservar" = "Me gustaría agendar" → INTENCIÓN DE AGENDAR
- "¿Cómo estás?" = "¿Todo bien?" = "¿Qué tal?" → CONVERSACIÓN SOCIAL
- "Necesito ayuda" = "Quiero hablar con alguien" → ESCALACIÓN HUMANA

**PRINCIPIO CLAVE:**
- USA tu comprensión natural del lenguaje
- NO busques palabras específicas, ENTIENDE la intención
- Reconoce variaciones, modismos y sinónimos automáticamente
- Si hay duda entre información vs social → prefiere información (buscar en base de datos)

**🚨 NUNCA INVENTES INFORMACIÓN DEL NEGOCIO:**
- Para cualquier dato del negocio → responde "KnowledgeAgent"
- Para conversación social → responde directamente como un asistente amigable

**FORMATO DE RESPUESTA:**
- Si es consulta informativa: responde "KnowledgeAgent - [descripción de la consulta]"
- Si es intención de agendar: responde "AppointmentAgent - [descripción de la intención]"
- Si es escalación: responde "EscalationAgent - [razón]"
- Si es conversación social: responde directamente con un saludo amigable (ej: "¡Hola! ¿En qué puedo ayudarte hoy? 😊")"""
    
    user_input_for_llm = f"""
    **MENSAJE DEL USUARIO A PROCESAR:**
    "{latest_user_message.content}"
    """

    # --- 4. Invocar el LLM (Supervisor) ---
    supervisor_agent = Agent[GlobalState](
        'openai:gpt-4o',
        deps_type=GlobalState,
        system_prompt=system_prompt
    )
    
    print(f"🔍 Invocando supervisor semántico para: '{latest_user_message.content}'")
    result = await supervisor_agent.run(user_input_for_llm, deps=state)
    
    # El LLM ahora responde usando análisis semántico natural (devuelve str directamente)
    response_text = result.data
    
    print(f"🧠 Supervisor analizó: '{response_text[:100]}...'")
    
    # Análisis inteligente de la respuesta del LLM
    response_lower = response_text.lower()
    
    # Detección semántica avanzada basada en la respuesta del LLM
    if "knowledgeagent" in response_lower or "knowledge_agent" in response_lower or "buscar información" in response_lower:
        print("📋 Análisis semántico: Consulta informativa → KnowledgeAgent")
        return Command(goto="KnowledgeAgent")
    elif "appointmentagent" in response_lower or "appointment_agent" in response_lower:
        print("📅 Análisis semántico: Intención de agendar → AppointmentAgent") 
        return Command(goto="AppointmentAgent")
    elif "escalationagent" in response_lower or "escalation_agent" in response_lower:
        print("🤝 Análisis semántico: Escalación → EscalationAgent")
        return Command(goto="EscalationAgent")
    elif "terminate" in response_lower or any(social in response_lower for social in ["hola", "bien", "gracias", "adiós"]):
        print("💬 Análisis semántico: Conversación social → Respuesta directa")
        ai_message = AIMessage(content=response_text, name="Supervisor")
        current_messages = state.get("messages", [])
        return Command(
            update={"messages": current_messages + [ai_message]},
            goto="__end__"
        )
    else:
        # Fallback inteligente: si hay duda, buscar información
        print("❓ Análisis semántico ambiguo → KnowledgeAgent (fallback inteligente)")
        return Command(goto="KnowledgeAgent")
