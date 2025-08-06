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
    Tu misión es ser un experto en formular preguntas para buscar información.
    1.  **Analiza la última pregunta del usuario** y el historial para entender su intención.
    2.  **Formula la consulta de búsqueda más clara y concisa posible** para la herramienta `knowledge_search`. Por ejemplo, si el usuario pregunta "¿y cuánto cuesta?", tu consulta debe ser "precio". Si preguntan "¿qué contraindicaciones tiene?", tu consulta debe ser "contraindicaciones".
    3.  La herramienta `knowledge_search` usará automáticamente el contexto del servicio actual si existe. No necesitas incluirlo en tu consulta.
    4.  Una vez que recibas los resultados, **rellena el modelo `KnowledgeSearchResult`** de la forma más completa posible. Asegúrate de incluir `service_name` si lo encuentras en los resultados.
    5.  **REGLA CRÍTICA:** Usa el campo `clarification_message` ÚNICAMENTE si la búsqueda NO arrojó resultados y necesitas pedirle al usuario que reformule su pregunta. Si encontraste información, este campo SIEMPRE debe quedar en `null`.
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
    
    result = await knowledge_agent.run(user_query, deps=state)
    
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

    return {"knowledge_result": tool_output}
