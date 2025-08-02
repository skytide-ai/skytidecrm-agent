from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field
import pydantic_ai
from openai import OpenAI
from pydantic_ai import Agent, RunContext
import asyncio

# Importamos el estado global y funciones de Zep
from ..state import GlobalState
from ..zep import get_zep_memory_context, search_zep_facts, search_zep_sessions, search_zep_nodes
from ..db import supabase_client

# --- Cliente Pydantic AI ---
# Usamos un cliente de OpenAI independiente para este agente
# para mantener los contextos de herramientas separados.
client = OpenAI()

# --- Funciones Auxiliares para Búsqueda Semántica ---

async def generate_embedding(text: str) -> List[float]:
    """
    Genera un embedding para el texto usando OpenAI.
    """
    try:
        response = client.embeddings.create(
            model="text-embedding-3-small",
            input=text
        )
        return response.data[0].embedding
    except Exception as e:
        print(f"❌ Error generando embedding: {e}")
        return []

async def search_knowledge_semantic(query: str, organization_id: str, limit: int = 5) -> List[Dict[str, Any]]:
    """
    Busca información (servicios y archivos) usando búsqueda semántica en la knowledge_base.
    
    Args:
        query: Consulta de búsqueda del usuario
        organization_id: ID de la organización 
        limit: Número máximo de resultados
    
    Returns:
        Lista de contenido encontrado (servicios y archivos) con sus metadatos
    """
    try:
        # Generar embedding de la consulta
        query_embedding = await generate_embedding(query)
        
        if not query_embedding:
            return []
        
        # Buscar en knowledge_base usando similarity search
        # Usamos la función match_documents de Supabase que maneja embeddings
        result = supabase_client.rpc(
            'match_documents',
            {
                'query_embedding': query_embedding,
                'match_count': limit,
                'filter': {'organization_id': organization_id}
            }
        ).execute()
        
        results_found = []
        seen_items = set()
        
        if result.data:
            for item in result.data:
                metadata = item.get('metadata', {})
                source_type = metadata.get('source_type')
                
                if source_type == 'service':
                    service_id = metadata.get('service_id')
                    # Evitar duplicados del mismo servicio
                    if service_id and service_id not in seen_items:
                        seen_items.add(service_id)
                        results_found.append({
                            'service_id': service_id,
                            'content': item.get('content', ''),
                            'similarity': item.get('similarity', 0),
                            'metadata': metadata
                        })
                        
                elif source_type == 'file':
                    # Para archivos, usamos una combinación única de file_name + category
                    file_name = metadata.get('file_name', '')
                    category = metadata.get('category', '')
                    unique_key = f"{file_name}_{category}"
                    
                    if unique_key not in seen_items:
                        seen_items.add(unique_key)
                        results_found.append({
                            'content': item.get('content', ''),
                            'similarity': item.get('similarity', 0),
                            'metadata': metadata
                        })
        
        return results_found
        
    except Exception as e:
        print(f"❌ Error en búsqueda semántica: {e}")
        return []

async def get_service_by_id(service_id: str) -> Optional[Dict[str, Any]]:
    """
    Obtiene información completa de un servicio por su ID.
    """
    try:
        result = supabase_client.table('services').select('*').eq('id', service_id).execute()
        
        if result.data:
            return result.data[0]
        return None
        
    except Exception as e:
        print(f"❌ Error obteniendo servicio {service_id}: {e}")
        return None



# --- Modelos de Respuesta ---

class KnowledgeSearchResult(BaseModel):
    """Modelo para el resultado de la búsqueda de conocimiento."""
    # Para servicios
    service_id: Optional[str] = Field(default=None, description="El ID único del servicio encontrado.")
    service_name: Optional[str] = Field(default=None, description="Nombre del servicio encontrado.")
    requires_assessment: Optional[bool] = Field(default=None, description="Si el servicio requiere valoración previa.")
    
    # Para información general (archivos)
    information_found: Optional[str] = Field(default=None, description="Información específica encontrada (ubicación, horarios, contacto, etc.)")
    source_type: Optional[str] = Field(default=None, description="Tipo de fuente: 'service' o 'file'")
    category: Optional[str] = Field(default=None, description="Categoría del archivo si es de tipo 'file'")
    
    # Para casos especiales
    clarification_message: Optional[str] = Field(default=None, description="Mensaje para pedir aclaración al usuario si la búsqueda es ambigua o no arroja resultados.")



class UserFactsResult(BaseModel):
    """Modelo para el resultado de la búsqueda de hechos del usuario."""
    facts_found: List[str] = Field(default_factory=list, description="Lista de hechos encontrados sobre el usuario.")
    summary: str = Field(description="Resumen de los hechos encontrados.")

class UserSessionsResult(BaseModel):
    """Modelo para el resultado de la búsqueda de conversaciones pasadas."""
    conversations_found: List[str] = Field(default_factory=list, description="Lista de fragmentos de conversaciones relevantes.")
    summary: str = Field(description="Resumen de las conversaciones encontradas.")

class UserInsightsResult(BaseModel):
    """Modelo para el resultado de la búsqueda de insights del usuario."""
    insights_found: List[str] = Field(default_factory=list, description="Lista de insights/resúmenes encontrados.")
    summary: str = Field(description="Resumen de los insights encontrados.")

# --- Definición del Agente de Conocimiento ---
knowledge_agent = Agent[GlobalState](
    'openai:gpt-4o', 
    deps_type=GlobalState,
    system_prompt="""
    Eres un asistente experto en la oferta de servicios de un centro de estética.
    Tu trabajo es ayudar al usuario a encontrar y seleccionar el servicio que desea, utilizando tanto la información actual como el historial del usuario.

    HERRAMIENTAS DISPONIBLES:
    
    1. BÚSQUEDA DE INFORMACIÓN:
       - 'knowledge_search': Busca servicios Y información general usando IA semántica
       - NO necesitas palabras exactas: busca por significado e intención
       - Encuentra automáticamente: ubicación, horarios, contacto, FAQ, políticas, promociones, etc.
    
    2. BÚSQUEDA DE HISTORIAL DEL USUARIO (úsalas cuando sea relevante):
       - 'search_user_facts': Busca hechos específicos (servicios previos, preferencias, alergias, etc.)
       - 'search_user_conversations': Busca conversaciones pasadas (recomendaciones, quejas, etc.)
       - 'search_user_insights': Busca patrones de comportamiento del usuario
    
    FLUJO DE TRABAJO:
    
    1. EVALÚA LA CONSULTA: ¿El usuario menciona algo del pasado, historial, o "antes"?
       - SI: Usa herramientas de búsqueda de historial primero
       - NO: Procede con búsqueda de servicios
    
    2. BÚSQUEDA DE INFORMACIÓN:
       - Usa 'knowledge_search' para encontrar servicios O información general
       - Para servicios: incluye service_id, nombre y si requiere valoración
       - Para info general: incluye contenido directo (ubicación, horarios, etc.)
       - Si necesitas aclaración, devuelve el mensaje y termina
    
    3. PERSONALIZACIÓN CON HISTORIAL:
       - Si es relevante, busca información previa del usuario
       - Combina la información actual con el historial para dar respuestas personalizadas
    
    EJEMPLOS DE USO:
    
    INFORMACIÓN GENERAL (búsqueda semántica inteligente):
    - "¿Dónde quedan?" / "dirección" / "ubicación" → Encuentra info de ubicación
    - "¿A qué hora abren?" / "horarios" / "cuándo atienden?" → Encuentra horarios
    - "¿Teléfono?" / "contacto" / "WhatsApp" → Encuentra info de contacto
    - "¿Ofertas?" / "descuentos" / "promociones" → Encuentra promociones
    - "¿Dudas frecuentes?" / "FAQ" / "preguntas" → Encuentra FAQ
    - "¿Políticas?" / "reglas" / "términos" → Encuentra políticas
    
    SERVICIOS:
    - "Quiero algo para rejuvenecer" → Busca servicios
    - "¿Cuánto cuesta el hidrofacial?" → Busca servicio específico
    
    HISTORIAL DEL USUARIO:
    - "¿Qué servicios me recomendaste antes?" → search_user_conversations
    - "¿Cuáles fueron mis servicios favoritos?" → search_user_facts
    - "¿Tuve algún problema?" → search_user_facts
    - "¿He venido antes por problemas de acné?"
    
    IMPORTANTE: No respondas directamente al usuario, solo ejecuta las herramientas necesarias y devuelve sus resultados estructurados.
    """
)

# --- Herramientas del Agente ---

@knowledge_agent.tool
async def knowledge_search(ctx: RunContext[GlobalState], query: str) -> KnowledgeSearchResult:
    """
    Busca cualquier información (servicios, ubicación, horarios, contacto, etc.) usando búsqueda semántica.
    Si encuentra información relevante, la devuelve. Si no, ofrece escalación a asesor.
    """
    state = ctx.deps
    organization_id = state.get("organization_id")
    
    if not organization_id:
        return KnowledgeSearchResult(clarification_message="Error: No se pudo identificar la organización.")
    
    print(f"🔍 Buscando información para: '{query}' en organización {organization_id}")
    
    try:
        # Buscar con límite de 3 resultados más relevantes
        matching_results = await search_knowledge_semantic(query, organization_id, limit=3)
        
        if not matching_results:
            return KnowledgeSearchResult(
                clarification_message=f"No encontré información específica sobre '{query}'. ¿Te gustaría hablar con un asesor que pueda ayudarte mejor?"
            )
        
        # Tomar el resultado más relevante (primero)
        best_result = matching_results[0]
        metadata = best_result.get('metadata', {})
        source_type = metadata.get('source_type')
        content = best_result.get('content', '')
        similarity = best_result.get('similarity', 0)
        
        print(f"✅ Información encontrada: source_type={source_type}, similarity={similarity:.2f}")
        print(f"📄 Content preview: {content[:100]}...")
        
        if source_type == 'file':
            # Es información general (ubicación, horarios, etc.)
            return KnowledgeSearchResult(
                information_found=content,
                source_type='file',
                category=metadata.get('category', 'general')
            )
        
        elif source_type == 'service':
            # Es información de un servicio
            service_id = metadata.get('service_id')
            service_data = await get_service_by_id(service_id)
            return KnowledgeSearchResult(
                service_id=service_id,
                service_name=service_data['name'] if service_data else None,
                requires_assessment=service_data['requiere_valoracion'] if service_data else None,
                source_type='service'
            )
        
        else:
            # Tipo de fuente desconocido, devolver contenido como información general
            return KnowledgeSearchResult(
                information_found=content,
                source_type='unknown'
            )
            
    except Exception as e:
        print(f"❌ Error en knowledge_search: {e}")
        return KnowledgeSearchResult(
            clarification_message="Hubo un problema al buscar información. ¿Te gustaría hablar con un asesor?"
        )


@knowledge_agent.tool
async def search_user_facts(ctx: RunContext[GlobalState], query: str) -> UserFactsResult:
    """
    Busca hechos específicos sobre el usuario actual en su historial.
    Útil para encontrar preferencias, servicios previos, problemas reportados, etc.
    
    Ejemplos de uso:
    - "servicios previos" 
    - "problemas de piel"
    - "preferencias de horarios"
    - "reacciones alérgicas"
    """
    state = ctx.deps
    print(f"🔍 Buscando hechos del usuario para: '{query}'")
    
    if not state.get("chat_identity_id"):
        return UserFactsResult(
            facts_found=[],
            summary="No hay información del usuario disponible en esta sesión."
        )
    
    # Construir user_id como en main.py
    if state.get("contact_id"):
        user_id = f"contact_{state['contact_id']}"
    else:
        user_id = f"chat_{state['chat_identity_id']}"
    
    try:
        facts = await search_zep_facts(user_id=user_id, query=query, limit=5)
        
        if facts:
            summary = f"Encontré {len(facts)} hechos relevantes sobre '{query}'"
            print(f"✅ {summary}")
            return UserFactsResult(facts_found=facts, summary=summary)
        else:
            summary = f"No encontré información específica sobre '{query}' en el historial del usuario."
            print(f"❌ {summary}")
            return UserFactsResult(facts_found=[], summary=summary)
            
    except Exception as e:
        print(f"❌ Error buscando hechos: {e}")
        return UserFactsResult(
            facts_found=[],
            summary="Error al buscar en el historial del usuario."
        )

@knowledge_agent.tool
async def search_user_conversations(ctx: RunContext[GlobalState], query: str) -> UserSessionsResult:
    """
    Busca conversaciones pasadas del usuario que contengan información específica.
    Útil para recordar discusiones previas, recomendaciones hechas, etc.
    
    Ejemplos de uso:
    - "citas canceladas"
    - "servicios recomendados"
    - "quejas o problemas"
    - "horarios preferidos"
    """
    state = ctx.deps
    print(f"🔍 Buscando conversaciones del usuario para: '{query}'")
    
    if not state.get("chat_identity_id"):
        return UserSessionsResult(
            conversations_found=[],
            summary="No hay historial de conversaciones disponible en esta sesión."
        )
    
    # Construir user_id como en main.py
    if state.get("contact_id"):
        user_id = f"contact_{state['contact_id']}"
    else:
        user_id = f"chat_{state['chat_identity_id']}"
    
    try:
        conversations = await search_zep_sessions(user_id=user_id, query=query, limit=3)
        
        if conversations:
            summary = f"Encontré {len(conversations)} conversaciones relevantes sobre '{query}'"
            print(f"✅ {summary}")
            return UserSessionsResult(conversations_found=conversations, summary=summary)
        else:
            summary = f"No encontré conversaciones previas sobre '{query}'."
            print(f"❌ {summary}")
            return UserSessionsResult(conversations_found=[], summary=summary)
            
    except Exception as e:
        print(f"❌ Error buscando conversaciones: {e}")
        return UserSessionsResult(
            conversations_found=[],
            summary="Error al buscar en el historial de conversaciones."
        )

@knowledge_agent.tool
async def search_user_insights(ctx: RunContext[GlobalState], query: str) -> UserInsightsResult:
    """
    Busca insights y resúmenes sobre el comportamiento del usuario.
    Útil para entender patrones, preferencias generales, perfil del cliente.
    
    Ejemplos de uso:
    - "perfil del cliente"
    - "comportamiento de compra"
    - "satisfacción con servicios"
    - "tendencias de uso"
    """
    state = ctx.deps
    print(f"🔍 Buscando insights del usuario para: '{query}'")
    
    if not state.get("chat_identity_id"):
        return UserInsightsResult(
            insights_found=[],
            summary="No hay información de insights disponible en esta sesión."
        )
    
    # Construir user_id como en main.py
    if state.get("contact_id"):
        user_id = f"contact_{state['contact_id']}"
    else:
        user_id = f"chat_{state['chat_identity_id']}"
    
    try:
        insights = await search_zep_nodes(user_id=user_id, query=query, limit=3)
        
        if insights:
            summary = f"Encontré {len(insights)} insights relevantes sobre '{query}'"
            print(f"✅ {summary}")
            return UserInsightsResult(insights_found=insights, summary=summary)
        else:
            summary = f"No encontré insights específicos sobre '{query}'."
            print(f"❌ {summary}")
            return UserInsightsResult(insights_found=[], summary=summary)
            
    except Exception as e:
        print(f"❌ Error buscando insights: {e}")
        return UserInsightsResult(
            insights_found=[],
            summary="Error al buscar insights del usuario."
        )

# --- Función de Entrada (Entrypoint) para el Grafo ---
async def run_knowledge_agent(state: GlobalState) -> Dict[str, Any]:
    """
    Punto de entrada para ejecutar el agente de conocimiento.
    Ahora incluye contexto de memoria de Zep.
    """
    print("--- Ejecutando Knowledge Agent ---")
    
    user_query = state['messages'][-1].content
    
    # Obtener contexto de memoria de Zep si hay chat_identity_id
    zep_context = ""
    if state.get("chat_identity_id"):
        thread_id = state['chat_identity_id']
        try:
            zep_context = await get_zep_memory_context(thread_id, min_rating=0.0)
        except Exception as e:
            print(f"❌ Error obteniendo contexto de Zep thread {thread_id}: {e}")
            zep_context = ""
    
    # Construir query enriquecido con contexto de Zep
    enhanced_query = user_query
    if zep_context:
        enhanced_query = f"Contexto del usuario: {zep_context}\n\nConsulta actual: {user_query}"
    
    result = await knowledge_agent.run(enhanced_query, deps=state)
    
    # El resultado del agente puede ser de varios tipos, lo procesamos
    tool_output = result.output

    if isinstance(tool_output, KnowledgeSearchResult):
        if tool_output.clarification_message:
            # Si la herramienta devuelve un mensaje de clarificación (incluso si falló),
            # lo tratamos como la respuesta final de este turno para romper el bucle.
            return {
                "messages": [("ai", tool_output.clarification_message)]
            }
        elif tool_output.service_id:
            # Si se encontró un servicio, incluir información de valoración si está disponible
            result_data = {"service_id": tool_output.service_id}
            if tool_output.requires_assessment is not None:
                result_data["requires_assessment"] = tool_output.requires_assessment
            if tool_output.service_name:
                result_data["service_name"] = tool_output.service_name
            return result_data
        elif tool_output.information_found:
            # Si se encontró información general (archivos), devolverla directamente
            return {
                "messages": [("ai", tool_output.information_found)],
                "next_agent": "terminate"  # Indicar que la tarea se completó
            }
    
    elif isinstance(tool_output, UserFactsResult):
        if tool_output.facts_found:
            facts_text = "\n".join([f"- {fact}" for fact in tool_output.facts_found])
            response_message = f"{tool_output.summary}\n\n{facts_text}"
            return {"messages": [("ai", response_message)]}
        else:
            # Si no se encuentran hechos, no se devuelve ningún mensaje para evitar bucles.
            return {}
    
    elif isinstance(tool_output, UserSessionsResult):
        if tool_output.conversations_found:
            conversations_text = "\n".join([f"- {conv}" for conv in tool_output.conversations_found])
            response_message = f"{tool_output.summary}\n\n{conversations_text}"
            return {"messages": [("ai", response_message)]}
        else:
            return {}

    elif isinstance(tool_output, UserInsightsResult):
        if tool_output.insights_found:
            insights_text = "\n".join([f"- {insight}" for insight in tool_output.insights_found])
            response_message = f"{tool_output.summary}\n\n{insights_text}"
            return {"messages": [("ai", response_message)]}
        else:
            return {}

    # Si el resultado es un string directo (respuesta del LLM)
    elif isinstance(tool_output, str):
        return {
            "messages": [("ai", tool_output)]
        }

    # Caso por defecto o si la salida no es lo que esperamos
    return {
        "messages": [("ai", "No estoy seguro de cómo proceder. ¿Puedes reformular tu pregunta?")]
    }