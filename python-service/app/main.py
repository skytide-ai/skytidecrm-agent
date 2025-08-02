import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage
from typing import Optional, Dict, Any

# Importamos nuestro estado global, nodos y el supervisor
from .state import GlobalState
from .agents.knowledge_agent import run_knowledge_agent
from .agents.appointment_agent import run_appointment_agent
from .agents.escalation_agent import run_escalation_agent
from .supervisor import supervisor_node, TERMINATE

# Importamos las funciones de Zep Cloud
from .zep import (
    zep_client,
    ensure_user_exists,
    ensure_thread_exists,
    add_messages_to_zep,
    get_zep_memory_context,
    update_zep_user_with_real_data
)

# --- 1. Funciones auxiliares del grafo ---

# Función eliminada - ya no necesaria con el patrón Command

# --- 2. Definición del Grafo de LangGraph ---
# El grafo se define igual, pero se compila sin checkpointer

workflow = StateGraph(GlobalState)

# Con el patrón Command moderno, el supervisor maneja el routing automáticamente
workflow.add_node("KnowledgeAgent", run_knowledge_agent)
workflow.add_node("AppointmentAgent", run_appointment_agent)
workflow.add_node("EscalationAgent", run_escalation_agent)
workflow.add_node("Supervisor", supervisor_node)

workflow.set_entry_point("Supervisor")

# Edges de regreso de agentes al supervisor
workflow.add_edge("KnowledgeAgent", "Supervisor")
workflow.add_edge("AppointmentAgent", "Supervisor") 
workflow.add_edge("EscalationAgent", "Supervisor")

# Función de routing para manejar respuestas directas
def route_supervisor(state: GlobalState):
    """Routing function para manejar respuestas directas del supervisor"""
    next_agent = state.get("next_agent")
    print(f"🔄 Routing supervisor - next_agent: {next_agent}")
    
    if next_agent == TERMINATE:
        print("🏁 Supervisor terminando con respuesta directa")
        return END
    else:
        print(f"🏁 Estado desconocido en routing, terminando: {next_agent}")
        return END

# Agregar conditional edges para el supervisor
workflow.add_conditional_edges(
    "Supervisor",
    route_supervisor,
    {
        TERMINATE: END,
        END: END
    }
)

# --- 2. Compilación del Grafo con Checkpointer ---
memory_saver = MemorySaver()
app_graph = workflow.compile(checkpointer=memory_saver)

# --- 3. Funciones auxiliares para Zep ---

async def get_contact_data(contact_id: str, organization_id: str) -> Optional[Dict[str, Any]]:
    """
    Obtiene los datos reales del contacto desde Supabase.
    
    Returns:
        Dict con first_name, last_name, phone, etc. o None si no se encuentra
    """
    try:
        from .db import supabase_client
        
        response = await supabase_client.table('contacts')\
            .select('first_name, last_name, phone, country_code')\
            .eq('id', contact_id)\
            .eq('organization_id', organization_id)\
            .maybe_single()\
            .execute()
        
        if response.data:
            return response.data
        else:
            print(f"❌ No se encontró contacto con ID: {contact_id}")
            return None
            
    except Exception as e:
        print(f"❌ Error obteniendo datos del contacto {contact_id}: {e}")
        return None




# --- 3. FastAPI App ---

class InvokePayload(BaseModel):
    organizationId: str
    chatIdentityId: str          # ⭐ NUEVO: Ya resuelto desde Gateway
    contactId: Optional[str]     # ⭐ NUEVO: Ya resuelto desde Gateway
    phone: str                   # Número completo (platform_user_id)
    phoneNumber: str             # Número nacional (dial_code)  
    countryCode: str             # Código de país (con +)
    message: str                 # Contenido del mensaje

app = FastAPI()

@app.get("/")
def read_root():
    return {"Hello": "World"}

@app.post("/chat")
async def chat(payload: InvokePayload):
    """
    Ruta de chat para pruebas y desarrollo.
    Alias de /invoke con el mismo comportamiento.
    """
    return await invoke(payload)

@app.post("/invoke")
async def invoke(payload: InvokePayload):
    session_id = payload.chatIdentityId  # Este es nuestro thread_id
    user_id = f"chat_{payload.chatIdentityId}" # ID de usuario único para Zep

    try:
        # 1. Validaciones básicas
        if not payload.message:
            return {"status": "error", "message": "No user input found in payload."}
        if not payload.organizationId or not payload.chatIdentityId:
            return {"status": "error", "message": "OrganizationId and ChatIdentityId are required."}

        print(f"--- Iniciando gestión para Thread: {session_id} ---")

        # 2. Gestión de Usuario y Thread en Zep (ANTES de invocar el grafo)
        # 2.1. Determinar datos del usuario
        first_name, last_name = "Usuario", ""
        if payload.contactId:
            contact_data = await get_contact_data(payload.contactId, payload.organizationId)
            if contact_data:
                first_name = contact_data.get("first_name", "Usuario")
                last_name = contact_data.get("last_name", "")
                print(f"✅ Datos del contacto obtenidos: {first_name} {last_name}")

        # 2.2. Asegurar que el usuario y el thread existen
        await ensure_user_exists(user_id, first_name, last_name, "")
        await ensure_thread_exists(session_id, user_id)
        
        # 2.3. Añadir el mensaje del USUARIO al historial de Zep ANTES de pensar
        from zep_cloud import Message
        user_message = Message(role="user", content=payload.message)
        await add_messages_to_zep(session_id, [user_message])
        print(f"✅ Mensaje del usuario añadido a Zep para el thread: {session_id}")

        # 3. Preparar e invocar el grafo
        config = {"configurable": {"thread_id": session_id}}
        
        # El estado inicial solo necesita los datos clave, LangGraph cargará el historial
        initial_state = GlobalState(
            messages=[HumanMessage(content=payload.message)], # Solo el mensaje actual para iniciar
            organization_id=payload.organizationId,
            chat_identity_id=payload.chatIdentityId,
            contact_id=payload.contactId,
            phone=payload.phone,
            phone_number=payload.phoneNumber,
            country_code=payload.countryCode
        )

        print(f"--- Invocando grafo para la sesión: {session_id} ---")
        # Agregar recursion_limit para evitar bucles infinitos
        config_with_limit = {**config, "recursion_limit": 50}
        
        try:
            final_state_result = await app_graph.ainvoke(initial_state, config_with_limit)
            print(f"--- Grafo finalizado para la sesión: {session_id} ---")
        except Exception as graph_error:
            print(f"❌ Error en grafo: {graph_error}")
            print(f"🔍 Intentando capturar el último estado válido...")
            
            # Usar stream para capturar estados intermedios
            last_valid_state = initial_state
            try:
                async for state_update in app_graph.astream(initial_state, config_with_limit):
                    print(f"📡 Estado capturado: {list(state_update.keys())}")
                    # Actualizar con el último estado válido
                    for node_name, node_state in state_update.items():
                        if node_state.get("messages"):
                            last_valid_state = node_state
                            print(f"💾 Estado guardado de {node_name}: {len(node_state['messages'])} mensajes")
            except Exception as stream_error:
                print(f"❌ Error en stream: {stream_error}")
            
            final_state_result = last_valid_state

        # 4. Extraer y guardar la respuesta de la IA
        ai_response_content = "No he podido procesar tu solicitud. Por favor, intenta de nuevo."
        
        # DEBUG: Información completa del estado final
        print(f"🔍 DEBUGGING - Estado final:")
        print(f"🔍 Tipo: {type(final_state_result)}")
        print(f"🔍 Claves disponibles: {list(final_state_result.keys()) if hasattr(final_state_result, 'keys') else 'No tiene keys'}")
        
        # Buscar el último mensaje AI en el estado final
        if final_state_result.get("messages"):
            messages = final_state_result["messages"]
            print(f"🔍 Total mensajes encontrados: {len(messages)}")
            for i, msg in enumerate(messages):
                print(f"🔍 Mensaje {i}: {type(msg).__name__} - {msg.content[:50]}...")
            
            last_message = messages[-1]
            if isinstance(last_message, AIMessage):
                ai_response_content = last_message.content
                print(f"✅ Usando último mensaje AI: {ai_response_content[:100]}...")
            else:
                print(f"⚠️ Último mensaje no es AIMessage: {type(last_message)}")
                print(f"🔍 Contenido del último mensaje: {last_message}")
        else:
            print(f"⚠️ No se encontraron mensajes en el estado final")
            print(f"🔍 Estado final completo: {final_state_result}")
        
        # 4.1. Añadir el mensaje de la IA a Zep
        ai_message = Message(role="assistant", content=ai_response_content)
        await add_messages_to_zep(session_id, [ai_message])
        print(f"✅ Mensaje de la IA añadido a Zep para el thread: {session_id}")

        # 5. Devolver la respuesta final
        return {"response": ai_response_content}

    except Exception as e:
        print(f"❌ Error crítico en endpoint /invoke para sesión {session_id}: {e}")
        # Loguear el traceback completo para depuración
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": "Internal server error. Please try again."}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000) 