from typing import TypedDict, Annotated, List, Optional, Any
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

class GlobalState(TypedDict):
    """
    Representa el estado compartido a través de todo el grafo de agentes.
    Este estado es persistente gracias al checkpointing.
    """
    messages: Annotated[List[BaseMessage], add_messages]
    organization_id: str
    phone: str
    phone_number: Optional[str]
    country_code: Optional[str]
    contact_id: Optional[str]
    chat_identity_id: Optional[str]
    service_id: Optional[str] = None
    service_name: Optional[str] = None
    requires_assessment: Optional[bool] = None
    available_slots: Optional[list] = None
    appointment_date_query: Optional[str] = None
    focused_appointment: Optional[dict] = None
    ready_to_book: Optional[bool] = None
    selected_date: Optional[str] = None
    selected_time: Optional[str] = None
    selected_member_id: Optional[str] = None
    next_agent: Optional[str] = None
    
    # --- CAMPO AÑADIDO PARA EL RESULTADO DEL KNOWLEDGE AGENT ---
    knowledge_result: Optional[Any] = None
