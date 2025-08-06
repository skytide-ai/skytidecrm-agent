import uvicorn
from fastapi import FastAPI, Request
from pydantic import BaseModel
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import HumanMessage, AIMessage
from typing import Optional, Dict, Any

# Importamos nuestro estado global y todos los nodos
from .state import GlobalState
from .agents.knowledge_agent import run_knowledge_agent
from .agents.appointment_agent import run_appointment_agent
from .agents.escalation_agent import run_escalation_agent
from .supervisor import supervisor_node, response_formatter_node

# Zep Cloud Imports
from .zep import add_messages_to_zep, ensure_user_exists, ensure_thread_exists
from .db import supabase_client

# --- 1. Definición del Grafo de LangGraph ---
workflow = StateGraph(GlobalState)

# Añadir todos los nodos al grafo
workflow.add_node("Supervisor", supervisor_node)
workflow.add_node("KnowledgeAgent", run_knowledge_agent)
workflow.add_node("AppointmentAgent", run_appointment_agent)
workflow.add_node("EscalationAgent", run_escalation_agent)
workflow.add_node("ResponseFormatter", response_formatter_node)

# --- 2. Definición de las Conexiones del Grafo ---
workflow.set_entry_point("Supervisor")

# El supervisor ahora devuelve un `Command(goto='...')` que LangGraph usa para enrutar.
# Ya no se necesita `add_conditional_edges`.

# Definimos las conexiones estáticas entre los agentes y los siguientes pasos.
workflow.add_edge("KnowledgeAgent", "ResponseFormatter")
workflow.add_edge("ResponseFormatter", END)
workflow.add_edge("AppointmentAgent", END)
workflow.add_edge("EscalationAgent", END)

# --- 3. Compilación del Grafo ---
memory_saver = MemorySaver()
app_graph = workflow.compile(checkpointer=memory_saver)

# --- 4. FastAPI App ---
class InvokePayload(BaseModel):
    organizationId: str
    chatIdentityId: str
    contactId: Optional[str]
    phone: str
    phoneNumber: str
    countryCode: str
    message: str

app = FastAPI()

async def get_contact_data(contact_id: str, organization_id: str) -> Optional[Dict[str, Any]]:
    try:
        response = await supabase_client.table('contacts').select('first_name, last_name').eq('id', contact_id).eq('organization_id', organization_id).maybe_single().execute()
        return response.data if response.data else None
    except Exception as e:
        print(f"❌ Error obteniendo datos del contacto {contact_id}: {e}")
        return None

@app.post("/invoke")
async def invoke(payload: InvokePayload, request: Request):
    session_id = payload.chatIdentityId
    user_id = f"contact_{payload.contactId}" if payload.contactId else f"chat_{session_id}"
    
    try:
        # Gestión de usuario y thread en Zep
        first_name, last_name = "Usuario", ""
        if payload.contactId:
            contact_data = await get_contact_data(payload.contactId, payload.organizationId)
            if contact_data:
                first_name = contact_data.get("first_name", "Usuario")
                last_name = contact_data.get("last_name", "")
        
        await ensure_user_exists(user_id, first_name, last_name, "")
        await ensure_thread_exists(session_id, user_id)
        
        from zep_cloud import Message
        await add_messages_to_zep(session_id, [Message(role="user", content=payload.message)])

        # Invocar el grafo
        config = {"configurable": {"thread_id": session_id}}
        initial_state = GlobalState(
            messages=[HumanMessage(content=payload.message)],
            organization_id=payload.organizationId,
            chat_identity_id=payload.chatIdentityId,
            contact_id=payload.contactId,
            phone=payload.phone,
            phone_number=payload.phoneNumber,
            country_code=payload.countryCode
        )
        
        final_state_result = await app_graph.ainvoke(initial_state, {**config, "recursion_limit": 50})

        # Extraer y guardar respuesta de IA
        ai_response_content = "No pude procesar tu solicitud."
        if final_state_result and final_state_result.get("messages"):
            last_message = final_state_result["messages"][-1]
            if isinstance(last_message, AIMessage):
                ai_response_content = last_message.content

        await add_messages_to_zep(session_id, [Message(role="assistant", content=ai_response_content)])

        return {"response": ai_response_content}

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": "Internal server error."}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
