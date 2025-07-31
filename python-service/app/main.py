import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage
from typing import Optional

# Importamos nuestro estado global, nodos y el supervisor
from .state import GlobalState
from .agents.knowledge_agent import run_knowledge_agent
from .agents.appointment_agent import run_appointment_agent
from .agents.escalation_agent import run_escalation_agent
from .supervisor import supervisor_node, TERMINATE

# Importamos el cliente de Zep
from .zep import zep_client

# --- 1. Definición del Grafo de LangGraph ---
# El grafo se define igual, pero se compila sin checkpointer

workflow = StateGraph(GlobalState)

workflow.add_node("KnowledgeAgent", run_knowledge_agent)
workflow.add_node("AppointmentAgent", run_appointment_agent)
workflow.add_node("EscalationAgent", run_escalation_agent)
workflow.add_node("Supervisor", supervisor_node)

workflow.set_entry_point("Supervisor")

workflow.add_edge("KnowledgeAgent", "Supervisor")
workflow.add_edge("AppointmentAgent", "Supervisor")
workflow.add_edge("EscalationAgent", "Supervisor")

def route_next(state: GlobalState):
    next_agent = state.get("next_agent")
    if next_agent == TERMINATE or not next_agent:
        return END
    return next_agent

workflow.add_conditional_edges(
    "Supervisor",
    route_next,
    {
        "KnowledgeAgent": "KnowledgeAgent",
        "AppointmentAgent": "AppointmentAgent",
        "EscalationAgent": "EscalationAgent",
        TERMINATE: END
    }
)

# --- 2. Compilación del Grafo con Checkpointer ---
memory_saver = MemorySaver()
app_graph = workflow.compile(checkpointer=memory_saver)


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

@app.post("/invoke")
async def invoke(payload: InvokePayload):
    try:
        session_id = payload.phone
        config = {"configurable": {"thread_id": session_id}}

        # 1. Validaciones básicas
        user_input = payload.message
        if not user_input:
            return {"status": "error", "message": "No user input found in payload."}
        
        if not payload.organizationId:
            return {"status": "error", "message": "Organization ID is required."}
            
        if not payload.chatIdentityId:
            return {"status": "error", "message": "Chat Identity ID is required."}

        # 2. Construir la entrada para el grafo con datos ya resueltos por el Gateway
        # El checkpointer se encargará de cargar el estado anterior si existe.
        initial_input = GlobalState(
            messages=[HumanMessage(content=user_input)],
            organization_id=payload.organizationId,
            chat_identity_id=payload.chatIdentityId,    # ⭐ NUEVO: Pre-resuelto
            contact_id=payload.contactId,               # ⭐ NUEVO: Pre-resuelto
            phone=payload.phone,                        # Número completo (platform_user_id)
            phone_number=payload.phoneNumber,           # Número nacional (dial_code)
            country_code=payload.countryCode            # Código de país (con +)
        )

        # 3. Invocar el grafo con la configuración del thread_id
        print(f"--- Invocando grafo para la sesión: {session_id} ---")
        print(f"    Chat Identity ID: {payload.chatIdentityId}")
        print(f"    Contact ID: {payload.contactId}")
        final_state_result = await app_graph.ainvoke(initial_input, config)
        print(f"--- Grafo finalizado para la sesión: {session_id} ---")

        final_state = final_state_result

        # 4. Extraer la respuesta final de la IA
        ai_response_content = ""
        if final_state.get("messages") and isinstance(final_state["messages"][-1], BaseMessage):
            last_message = final_state["messages"][-1]
            if isinstance(last_message, AIMessage):
                ai_response_content = last_message.content

        if not ai_response_content:
            ai_response_content = "No he podido procesar tu solicitud. Por favor, intenta de nuevo."

        # 5. Guardar SOLO los mensajes en Zep (sin el estado)
        # LangGraph y el checkpointer ya se encargaron de la persistencia del estado.
        try:
            from zep_python import Message
            
            user_message_to_save = Message(role="human", content=user_input)
            ai_message_to_save = Message(
                role="ai",
                content=ai_response_content,
                # Ya no guardamos el estado en los metadatos
            )
            
            # Obtenemos la memoria existente para no duplicar el último mensaje
            memory = await zep_client.memory.get_memory(session_id)
            if not memory or not any(m.content == user_input for m in memory.messages):
                 await zep_client.memory.add_memory(
                    session_id,
                    messages=[user_message_to_save, ai_message_to_save]
                )
            else:
                 await zep_client.memory.add_memory(
                    session_id,
                    messages=[ai_message_to_save]
                )

            print(f"Historial de mensajes guardado en Zep para la sesión: {session_id}")
        except Exception as e:
            print(f"Error al guardar mensajes en Zep para la sesión {session_id}: {e}")
        
        # 6. Devolver la respuesta al Gateway
        return {"response": ai_response_content}
        
    except Exception as e:
        print(f"❌ Error crítico en endpoint /invoke para sesión {session_id}: {e}")
        return {"status": "error", "message": "Internal server error. Please try again."}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000) 