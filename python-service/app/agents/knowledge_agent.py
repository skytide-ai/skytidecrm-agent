from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field
from uuid import UUID
from openai import AsyncOpenAI
from pydantic_ai import Agent, RunContext
import asyncio
import json

from ..state import GlobalState
from ..db import supabase_client

client = AsyncOpenAI()

async def generate_embedding(text: str) -> List[float]:
    try:
        response = await client.embeddings.create(model="text-embedding-3-small", input=text)
        return response.data[0].embedding
    except Exception as e:
        print(f"❌ Error generando embedding: {e}")
        return []

async def search_knowledge_semantic(query: str, organization_id: str, service_id: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
    try:
        query_embedding = await generate_embedding(query)
        if not query_embedding:
            return []
        
        rpc_params = {
            'query_embedding': query_embedding,
            'match_threshold': 0.2,
            'match_count': limit,
            'org_id': organization_id,
            'p_service_id': service_id  # CORREGIDO: Usar el nombre de parámetro correcto
        }
        
        print(f"🔍 Parámetros RPC: org_id={organization_id}, p_service_id={service_id}")

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, 
            lambda: supabase_client.rpc('match_documents_by_org', rpc_params).execute()
        )
        return result.data if result.data else []
    except Exception as e:
        print(f"❌ Error en búsqueda semántica: {e}")
        return []

class RawDataResult(BaseModel):
    """Contenedor simple para los datos crudos de la búsqueda."""
    results: List[Dict[str, Any]] = Field(description="Lista de resultados de la base de datos.")

class KnowledgeSearchResult(BaseModel):
    """Modelo para el resultado final del agente de conocimiento."""
    service_id: Optional[UUID] = Field(default=None)
    service_name: Optional[str] = Field(default=None)
    raw_information: Optional[str] = Field(default=None)
    source_type: Optional[str] = Field(default=None)
    category: Optional[str] = Field(default=None)
    clarification_message: Optional[str] = Field(default=None)


knowledge_agent = Agent[GlobalState, KnowledgeSearchResult](
    'openai:gpt-4o', 
    deps_type=GlobalState,
    output_type=KnowledgeSearchResult,
    system_prompt="""
    Tu misión es doble: actuar como un motor de búsqueda inteligente Y como un asistente conversacional que entiende la intención.

    **FASE 1: ANÁLISIS DE INTENCIÓN Y BÚSQUEDA**
    1.  **Analiza la pregunta del usuario:** ¿Está pidiendo información general ("qué servicios tienen"), información específica de un servicio ("cuánto cuesta la limpieza facial"), o confirmando su interés en un servicio para agendar?
    2.  **Formula la consulta de búsqueda:** Crea la query más efectiva para la herramienta `knowledge_search`. Sé conciso.

    **FASE 2: INTERPRETACIÓN DE RESULTADOS Y RESPUESTA**
    1.  **Analiza los resultados de la búsqueda:**
        - **Si NO hay resultados:** Usa `clarification_message` para pedir al usuario que reformule su pregunta.
        - **Si hay resultados:** Procede a rellenar el `KnowledgeSearchResult`.
    2.  **REGLA DE ORO - CONTEXTO DE AGENDAMIENTO:**
        - Si el historial de conversación (que se te proporciona) indica que la intención principal es **AGENDAR**, tu objetivo NO es simplemente devolver la información.
        - En este caso, debes:
            a. Rellenar `service_id` y `service_name` con el servicio más relevante encontrado.
            b. En `raw_information`, NO devuelvas toda la información. En su lugar, crea un mensaje de transición conversacional.
            - **Ejemplo 1:** Si el usuario pregunta "¿tienen limpieza facial?", y la intención es agendar, tu `raw_information` debería ser algo como: "¡Sí, claro! Tenemos el servicio de Limpieza Facial Profunda. ¿Te gustaría que te dé más detalles o procedemos a buscar una fecha para agendar?"
            - **Ejemplo 2:** Si el usuario pregunta "¿cuánto cuesta?", y la intención es agendar, tu `raw_information` debería ser: "El precio es de $90.000 COP. ¿Te gustaría buscar una fecha para este servicio?"
    3.  **SI LA INTENCIÓN ES SOLO INFORMATIVA:** Si el usuario solo parece estar preguntando por curiosidad (ej: "¿qué contraindicaciones tiene?"), entonces sí puedes devolver la información específica que encontró la herramienta en `raw_information`.
    """
)

@knowledge_agent.tool
async def knowledge_search(ctx: RunContext[GlobalState], query: str) -> RawDataResult:
    """Busca en la base de datos y devuelve una lista de resultados crudos."""
    print(f"--- 🛠️ Herramienta: knowledge_search ---")
    
    state = ctx.deps
    organization_id = state.get("organization_id")
    service_id = state.get("service_id") # <-- OBTENER SERVICE_ID DEL ESTADO
    
    # Si estamos en un contexto de servicio, lo usamos para filtrar la búsqueda
    if service_id:
        print(f"✅ Búsqueda con contexto de servicio: service_id='{service_id}'")
    
    print(f"Query para el LLM: '{query}'")
    
    if not organization_id:
        print("Resultado: No se encontró organization_id.")
        return RawDataResult(results=[])
    
    matching_results = await search_knowledge_semantic(query, organization_id, service_id=service_id)
    print(f"Resultado: Encontrados {len(matching_results)} resultados.")
    return RawDataResult(results=matching_results)


async def run_knowledge_agent(state: GlobalState):
    """Ejecuta el agente de conocimiento y guarda el resultado en el estado."""
    print("--- Ejecutando Knowledge Agent (Con Logs) ---")
    user_query = state['messages'][-1].content
    
    # Construir un prompt enriquecido con el historial de la conversación
    history = "\n".join([f"{msg.type}: {msg.content}" for msg in state.get("messages", [])])
    
    input_prompt = f"""
    Historial de la Conversación:
    ---
    {history}
    ---

    Estado Actual (para tu contexto, no para el usuario):
    - Servicio en foco (ID): {state.get('service_id')}
    
    Analiza el historial y el último mensaje del usuario para determinar la mejor acción.
    Último mensaje del usuario: "{user_query}"
    """
    
    result = await knowledge_agent.run(input_prompt, deps=state)
    
    tool_output = result.output
    
    print("\n--- DEBUG Knowledge Agent ---")
    print(f"Tipo de Salida del Agente: {type(tool_output)}")
    
    # Usar json.dumps para una visualización más limpia del objeto pydantic
    try:
        # Convertir UUID a string para serialización
        if isinstance(tool_output, KnowledgeSearchResult) and tool_output.service_id:
            tool_output.service_id = str(tool_output.service_id)
        
        print(f"Contenido de la Salida:\n{json.dumps(tool_output.dict(), indent=2)}")
    except Exception:
        print(f"Contenido de la Salida (no serializable): {tool_output}")
    print("---------------------------\n")

    if not isinstance(tool_output, KnowledgeSearchResult):
        tool_output = KnowledgeSearchResult(
            clarification_message="Lo siento, hubo un error interno al procesar la información."
        )
        print("⚠️ Salida del agente no fue `KnowledgeSearchResult`. Creando mensaje de error.")

    update_data = {"knowledge_result": tool_output}

    # Si encontramos un servicio, actualizamos el estado del flujo de agendamiento
    if tool_output.service_id:
        print(f"✅ Servicio encontrado. Actualizando booking_status a 'NEEDS_DATE'")
        update_data["booking_status"] = "NEEDS_DATE"

    return update_data
