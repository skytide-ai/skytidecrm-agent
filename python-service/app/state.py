from typing import TypedDict, Annotated, List, Optional
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

class GlobalState(TypedDict):
    """
    Representa el estado compartido a través de todo el grafo de agentes.
    Este estado es persistente gracias al checkpointing.
    """
    messages: Annotated[List[BaseMessage], add_messages]
    organization_id: str
    phone: str # Número completo (platform_user_id) que viene desde Gupshup
    phone_number: Optional[str] # Número nacional (dial_code) separado por Gupshup
    country_code: Optional[str] # Código de país (con +) desde Gupshup
    contact_id: Optional[str]
    chat_identity_id: Optional[str]
    service_id: Optional[str] = None
    requires_assessment: Optional[bool] = None
    available_slots: Optional[list] = None
    appointment_date_query: Optional[str] = None
    focused_appointment: Optional[dict] = None # Para guardar la cita que se está discutiendo
    next_agent: Optional[str] = None 