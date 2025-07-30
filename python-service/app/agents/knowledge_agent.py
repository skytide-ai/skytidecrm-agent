from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field
import pydantic_ai
from openai import OpenAI
from pydantic_ai import Agent, RunContext

# Importamos el estado global
from ..state import GlobalState

# --- Cliente Pydantic AI ---
# Usamos un cliente de OpenAI independiente para este agente
# para mantener los contextos de herramientas separados.
client = OpenAI()

# --- Simulación de la Base de Datos ---
MOCK_SERVICES_DB = [
    {'service_id': 'SVC-001', 'name': 'Limpieza facial - 5 sesiones', 'requires_assessment': False},
    {'service_id': 'SVC-002', 'name': 'Limpieza facial - 10 sesiones', 'requires_assessment': False},
    {'service_id': 'SVC-003', 'name': 'Masaje relajante', 'requires_assessment': False},
    {'service_id': 'SVC-004', 'name': 'Evaluación dermatológica', 'requires_assessment': True},
]

# --- Definición del Agente de Conocimiento ---
knowledge_agent = Agent(
    'openai:gpt-4-turbo', 
    system_prompt="""
    Eres un asistente experto en la oferta de servicios de un centro de estética.
    Tu trabajo es ayudar al usuario a encontrar y seleccionar el servicio que desea.
    - Primero, usa siempre la herramienta 'knowledge_search' para buscar servicios basados en la petición del usuario.
    - Si la búsqueda devuelve un `service_id` único, tu siguiente y último paso es usar la herramienta 'check_service_assessment' para ver si necesita una valoración previa.
    - Si la búsqueda requiere aclaración, devuelve el mensaje de aclaración al usuario y termina.
    - No respondas directamente al usuario, solo ejecuta las herramientas en el orden correcto y devuelve sus resultados.
    """
)

# --- Herramientas del Agente ---

class KnowledgeSearchResult(BaseModel):
    """Modelo para el resultado de la búsqueda de conocimiento."""
    service_id: Optional[str] = Field(default=None, description="El ID único del servicio encontrado.")
    clarification_message: Optional[str] = Field(default=None, description="Mensaje para pedir aclaración al usuario si la búsqueda es ambigua o no arroja resultados.")

@knowledge_agent.tool
def knowledge_search(query: str) -> KnowledgeSearchResult:
    """
    Busca en la base de datos de servicios basándose en una consulta de texto.
    """
    print(f"Buscando servicios que coincidan con: '{query}'")
    query_lower = query.lower()
    matching_services = [s for s in MOCK_SERVICES_DB if query_lower in s['name'].lower()]
    
    if len(matching_services) == 1:
        service = matching_services[0]
        print(f"Servicio único encontrado: {service['name']}")
        return KnowledgeSearchResult(service_id=service['service_id'])
    
    elif len(matching_services) > 1:
        print(f"Múltiples servicios encontrados para '{query}'. Pidiendo aclaración.")
        options_str = ", ".join([f"'{s['name']}'" for s in matching_services])
        clarification = f"Encontré varias opciones para '{query}': {options_str}. ¿Cuál te gustaría?"
        return KnowledgeSearchResult(clarification_message=clarification)
        
    else:
        print(f"No se encontraron servicios para '{query}'.")
        clarification = f"Lo siento, no pude encontrar ningún servicio que coincida con '{query}'. ¿Te gustaría intentar con otra búsqueda?"
        return KnowledgeSearchResult(clarification_message=clarification)

class AssessmentResult(BaseModel):
    """Modelo para el resultado de la verificación de valoración."""
    service_id: str
    requires_assessment: bool

@knowledge_agent.tool
def check_service_assessment(service_id: str) -> AssessmentResult:
    """
    Verifica si un servicio específico (dado su ID) requiere una valoración previa.
    """
    print(f"Verificando si el servicio {service_id} requiere valoración...")
    for service in MOCK_SERVICES_DB:
        if service['service_id'] == service_id:
            requires_assessment = service['requires_assessment']
            print(f"El servicio requiere valoración: {requires_assessment}")
            return AssessmentResult(service_id=service_id, requires_assessment=requires_assessment)
    # Esto no debería pasar si el service_id es válido.
    raise ValueError(f"Servicio con ID {service_id} no encontrado.")

# --- Función de Entrada (Entrypoint) para el Grafo ---
async def run_knowledge_agent(state: GlobalState) -> Dict[str, Any]:
    """
    Punto de entrada para ejecutar el agente de conocimiento.
    """
    print("--- Ejecutando Knowledge Agent ---")
    
    user_query = state['messages'][-1].content
    
    result = await knowledge_agent.run(user_query, client=client)
    
    # El resultado del agente puede ser de varios tipos, lo procesamos
    tool_output = result.output

    if isinstance(tool_output, AssessmentResult):
        # El agente encontró un servicio y verificó la valoración
        return {
            "service_id": tool_output.service_id,
            "requires_assessment": tool_output.requires_assessment
        }
    elif isinstance(tool_output, KnowledgeSearchResult):
        if tool_output.clarification_message:
            # El agente necesita aclarar algo con el usuario
            return {
                "messages": [("ai", tool_output.clarification_message)]
            }
        # Este caso (KnowledgeSearchResult con service_id) es intermedio,
        # el agente debería haber llamado a check_service_assessment.
        # Si llegamos aquí, es un estado inesperado, pero lo manejamos.
        elif tool_output.service_id:
             return {"service_id": tool_output.service_id}

    # Caso por defecto o si la salida no es lo que esperamos
    return {
        "messages": [("ai", "No estoy seguro de cómo proceder. ¿Puedes reformular tu pregunta?")]
    } 