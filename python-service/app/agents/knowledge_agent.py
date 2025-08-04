from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field
import pydantic_ai
from openai import AsyncOpenAI
from pydantic_ai import Agent, RunContext
import asyncio

# Importamos el estado global y funciones de Zep
from ..state import GlobalState
from ..zep import get_zep_memory_context, search_zep_facts, search_zep_sessions, search_zep_nodes
from ..db import supabase_client

# --- Cliente Pydantic AI ---
# Usamos un cliente de OpenAI independiente para este agente
# para mantener los contextos de herramientas separados.
client = AsyncOpenAI()

# --- Funciones Auxiliares para Búsqueda Semántica ---

async def generate_embedding(text: str) -> List[float]:
    """
    Genera un embedding para el texto usando OpenAI.
    """
    try:
        response = await client.embeddings.create(
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
        import time
        start_time = time.time()
        print(f"[{start_time:.2f}] --- Iniciando búsqueda semántica para: '{query}'")

        # Generar embedding de la consulta (esta función sí es async y debe ser esperada)
        query_embedding = await generate_embedding(query)
        
        if not query_embedding:
            return []
        
        embedding_time = time.time()
        print(f"[{embedding_time:.2f}] --- Embedding generado en {embedding_time - start_time:.2f}s")

        # Buscar en knowledge_base usando la sintaxis correcta para ejecutar
        # una llamada síncrona (rpc) desde un contexto asíncrono.
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, 
            lambda: supabase_client.rpc(
                'match_documents_by_org',
                {
                    'query_embedding': query_embedding,
                    'match_threshold': 0.2,
                    'match_count': limit,
                    'org_id': organization_id
                }
            ).execute()
        )

        rpc_time = time.time()
        print(f"[{rpc_time:.2f}] --- RPC a Supabase completado en {rpc_time - embedding_time:.2f}s")
        print(f"🔍 DEBUG: RPC retornó {len(result.data) if result.data else 0} resultados")

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
        
        end_time = time.time()
        print(f"[{end_time:.2f}] --- Búsqueda semántica completada en {end_time - start_time:.2f}s")
        return results_found
        
    except Exception as e:
        print(f"❌ Error en búsqueda semántica: {e}")
        return []

async def get_service_by_id(service_id: str) -> Optional[Dict[str, Any]]:
    """
    Obtiene información completa de un servicio por su ID.
    """
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: supabase_client.table('services').select('*').eq('id', service_id).execute()
        )
        
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
    Eres un asistente amigable y experto en servicios y productos de la empresa.
    Habla de manera natural y conversacional, como si fueras un asesor personal que conoce bien los servicios.
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
       - Para servicios: incluye nombre, descripción y detalles relevantes
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
    - "Quiero un servicio que me ayude con [problema]" → Busca servicios
    - "¿Cuánto cuesta [nombre del servicio]?" → Busca servicio específico
    
    HISTORIAL DEL USUARIO:
    - "¿Qué servicios me recomendaste antes?" → search_user_conversations
    - "¿Cuáles fueron mis servicios favoritos?" → search_user_facts
    - "¿Tuve algún problema?" → search_user_facts
    - "¿He venido antes por este tipo de problema?"
    
    🚨 REGLAS CRÍTICAS:
    - NUNCA INVENTES información que no encuentres en las herramientas
    - NO menciones servicios específicos a menos que la herramienta los devuelva explícitamente
    - Si no encuentras información específica, pide clarificación o ofrece escalación
    - SOLO usa datos reales retornados por las herramientas de búsqueda
    - NO hagas suposiciones sobre qué servicios podrían existir
    
    📝 FORMATO DE RESPUESTAS:
    - Habla de manera natural y conversacional, evita listas técnicas
    - Usa un tono amigable como "Te cuento que...", "Mira, tenemos...", "Perfecto, aquí está..."
    - Organiza la información de manera fluida, no como puntos de lista
    - Cuando presentes múltiples servicios, hazlo como si estuvieras recomendando personalmente
    - USA EMOJIS para hacer la conversación más natural y amigable 😊
    - NO uses asteriscos (**texto**) ni formato markdown para títulos
    - Escribe los nombres de servicios de forma natural, como en una conversación normal
    
    🚨 OBLIGATORIO: SIEMPRE debes usar las herramientas disponibles para buscar información. NUNCA respondas directamente sin usar herramientas.
    
    - Para CUALQUIER consulta sobre servicios, ubicación, horarios, precios → USA 'knowledge_search'
    - Para preguntas sobre historial del usuario → USA las herramientas de búsqueda de usuario
    - NO tengas conversaciones directas, SIEMPRE delega a las herramientas correspondientes
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
        # Buscar con límite más alto para capturar todos los chunks de un servicio
        matching_results = await search_knowledge_semantic(query, organization_id, limit=10)
        
        if not matching_results:
            return KnowledgeSearchResult(
                clarification_message=f"No encontré información específica sobre '{query}'. ¿Te gustaría hablar con un asesor que pueda ayudarte mejor?"
            )
        
        # Analizar si es una búsqueda amplia de servicios
        query_lower = query.lower()
        is_broad_service_query = any(keyword in query_lower for keyword in [
            'servicios', 'qué tienen', 'qué ofrecen', 'catálogo', 'tratamientos', 
            'ofertas', 'que hacen', 'especialidades', 'procedimientos'
        ])
        
        # Analizar el tipo de resultado y consolidar información si es necesario
        best_result = matching_results[0]
        metadata = best_result.get('metadata', {})
        source_type = metadata.get('source_type')
        similarity = best_result.get('similarity', 0)
        
        print(f"✅ Información encontrada: source_type={source_type}, similarity={similarity:.2f}")
        print(f"🔍 Búsqueda amplia de servicios detectada: {is_broad_service_query}")
        
        # 🎯 UMBRAL DE SIMILARITY: Si la similarity es muy baja, no es información útil
        if similarity < 0.1:  # Ajustar este valor según necesidades
            return KnowledgeSearchResult(
                clarification_message=f"No encontré información específica sobre '{query}'. ¿Podrías ser más específico sobre qué tipo de información necesitas? Por ejemplo: servicios, precios, ubicación, horarios, etc. Si prefieres, también puedo ayudarte a contactar con un asesor."
            )
        
        # 📋 MANEJO ESPECIAL PARA BÚSQUEDAS AMPLIAS DE SERVICIOS
        if is_broad_service_query and source_type == 'service':
            # Obtener servicios únicos de los resultados
            unique_services = {}
            for result in matching_results:
                result_metadata = result.get('metadata', {})
                if result_metadata.get('source_type') == 'service':
                    service_id = result_metadata.get('service_id')
                    similarity_score = result.get('similarity', 0)
                    
                    # Solo incluir si tiene buena similarity
                    if similarity_score >= 0.1 and service_id:
                        if service_id not in unique_services or similarity_score > unique_services[service_id]['similarity']:
                            unique_services[service_id] = {
                                'content': result.get('content', ''),
                                'similarity': similarity_score,
                                'metadata': result_metadata
                            }
            
            # Si tenemos múltiples servicios, mostrarlos todos
            if len(unique_services) > 1:
                services_info = []
                for service_id, service_data in unique_services.items():
                    content = service_data['content']
                    # Extraer nombre del servicio de los metadatos o contenido
                    service_name = service_data['metadata'].get('service_name', 'Servicio')
                    services_info.append(f"🌟 {service_name}\n{content}")
                
                consolidated_services = "\n\n".join(services_info)
                
                # Agregar sugerencia inteligente para más detalles
                suggestion = "\n\n💡 Si quieres información más detallada sobre algún servicio específico (como precios, contraindicaciones, o cuidados), solo menciona el nombre del servicio que te interesa 😊"
                
                return KnowledgeSearchResult(
                    information_found=consolidated_services + suggestion,
                    source_type='multiple_services',
                    category='servicios'
                )
            
            # Si solo hay un servicio pero era una búsqueda amplia, sugerir más opciones
            elif len(unique_services) == 1:
                # Procesar el único servicio encontrado pero agregar sugerencia
                service_data = list(unique_services.values())[0]
                content = service_data['content']
                suggestion = "\n\n💡 Este es uno de nuestros servicios. Si buscas algo específico o quieres conocer otros servicios disponibles, puedes preguntarme por el tipo de tratamiento que te interesa 😊"
                
                return KnowledgeSearchResult(
                    information_found=content + suggestion,
                    source_type='service',
                    service_id=list(unique_services.keys())[0]
                )
        
        if source_type == 'file':
            # Es información general (ubicación, horarios, etc.) - usar solo el mejor resultado
            content = best_result.get('content', '')
            print(f"📄 Content preview: {content[:100]}...")
            return KnowledgeSearchResult(
                information_found=content,
                source_type='file',
                category=metadata.get('category', 'general')
            )
        
        elif source_type == 'service':
            # Es información de un servicio - CONSOLIDAR TODOS LOS CHUNKS DEL MISMO SERVICIO
            service_id = metadata.get('service_id')
            service_name = metadata.get('service_name', 'Servicio')
            
            print(f"🔍 DEBUG: Buscando chunks para service_id={service_id}")
            print(f"🔍 DEBUG: Total de resultados recibidos: {len(matching_results)}")
            
            # DEBUG: Mostrar todos los resultados para diagnosticar
            for i, result in enumerate(matching_results):
                result_metadata = result.get('metadata', {})
                result_service_id = result_metadata.get('service_id')
                result_source_type = result_metadata.get('source_type')
                result_similarity = result.get('similarity', 0)
                print(f"🔍 DEBUG: Resultado {i+1} - service_id={result_service_id}, source_type={result_source_type}, similarity={result_similarity:.2f}")
            
            # Filtrar todos los resultados del mismo servicio (con similarity mínima)
            service_chunks = []
            for r in matching_results:
                r_metadata = r.get('metadata', {})
                r_service_id = r_metadata.get('service_id')
                r_source_type = r_metadata.get('source_type')
                r_similarity = r.get('similarity', 0)
                
                # Incluir si es del mismo servicio Y tiene buena similarity
                if (r_source_type == 'service' and 
                    r_service_id == service_id and 
                    r_similarity >= 0.1):
                    service_chunks.append(r)
            
            print(f"📋 DEBUG: Chunks del mismo servicio encontrados: {len(service_chunks)}")
            
            # Consolidar toda la información del servicio
            consolidated_content = []
            for chunk in service_chunks:
                chunk_content = chunk.get('content', '')
                chunk_similarity = chunk.get('similarity', 0)
                print(f"📄 Chunk encontrado (similitud: {chunk_similarity:.2f}): {chunk_content[:80]}...")
                consolidated_content.append(chunk_content)
            
            # Si no encontramos chunks adicionales, buscar otros chunks de servicio con similarity alta
            if len(service_chunks) == 1:
                print("🔍 Solo se encontró 1 chunk, buscando otros chunks de servicios relacionados...")
                for r in matching_results:
                    r_metadata = r.get('metadata', {})
                    r_source_type = r_metadata.get('source_type')
                    r_similarity = r.get('similarity', 0)
                    
                    # Incluir otros chunks de servicios con alta similarity que no hayamos incluido ya
                    if (r_source_type == 'service' and 
                        r_similarity >= 0.3 and  # Similarity más alta para otros servicios
                        r not in service_chunks):
                        
                        chunk_content = r.get('content', '')
                        print(f"📄 Chunk adicional relacionado (similitud: {r_similarity:.2f}): {chunk_content[:80]}...")
                        consolidated_content.append(chunk_content)
            
            # Unir todo el contenido con formato más conversacional
            full_service_info = "\n\n".join(consolidated_content)
            print(f"📋 Información consolidada del servicio ({len(consolidated_content)} chunks)")
            
            # Formatear de manera más conversacional
            conversational_info = f"Te cuento sobre {service_name} ✨\n\n{full_service_info}"
            
            return KnowledgeSearchResult(
                information_found=conversational_info,
                source_type='service',
                service_id=service_id
            )
        
        else:
            # Tipo de fuente desconocido, devolver contenido como información general
            content = best_result.get('content', '')
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
    - "problemas reportados"
    - "preferencias de horarios"
    - "alergias o restricciones"
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
from langgraph.types import Command
from langgraph.graph import END

async def run_knowledge_agent(state: GlobalState) -> Command:
    """
    Punto de entrada para ejecutar el agente de conocimiento.
    Ahora incluye contexto de memoria de Zep y usa Command pattern para evitar loops.
    """
    print("--- Ejecutando Knowledge Agent ---")
    
    user_query = state['messages'][-1].content
    
    # Detectar si venimos del AppointmentAgent
    is_service_resolution = "Necesito encontrar el servicio específico que quiere agendar" in user_query
    print(f"🔍 KNOWLEDGE AGENT: Modo resolución de servicio: {is_service_resolution}")
    
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
    if is_service_resolution and zep_context:
        # Cuando venimos del appointment_agent, extraer el servicio del contexto
        enhanced_query = f"Buscar servicio específico mencionado en: {zep_context}"
        print(f"🔍 KNOWLEDGE AGENT: Query de resolución: {enhanced_query}")
    else:
        enhanced_query = user_query
        if zep_context:
            enhanced_query = f"Contexto del usuario: {zep_context}\n\nConsulta actual: {user_query}"
    
    result = await knowledge_agent.run(enhanced_query, deps=state)
    
    # El resultado del agente puede ser de varios tipos, lo procesamos
    tool_output = result.output
    print(f"🔍 DEBUG: tool_output type: {type(tool_output)}")
    print(f"🔍 DEBUG: tool_output value: {tool_output}")

    # Obtener mensajes actuales para conservar el historial
    current_messages = state.get("messages", [])

    if isinstance(tool_output, KnowledgeSearchResult):
        print(f"🔍 DEBUG KnowledgeSearchResult:")
        print(f"🔍 clarification_message: {bool(tool_output.clarification_message)}")
        print(f"🔍 service_id: {tool_output.service_id}")
        print(f"🔍 information_found: {bool(tool_output.information_found)}")
        
        if tool_output.clarification_message:
            # Si la herramienta devuelve un mensaje de clarificación (incluso si falló),
            # lo tratamos como la respuesta final de este turno para romper el bucle.
            from langchain_core.messages import AIMessage
            ai_message = AIMessage(content=tool_output.clarification_message, name="KnowledgeAgent")
            return Command(
                update={"messages": current_messages + [ai_message]},
                goto="__end__"
            )
        elif tool_output.service_id:
            # Si se encontró un servicio, incluir información de valoración si está disponible
            # y enrutar al AppointmentAgent para posible agendamiento
            print(f"📋 KNOWLEDGE AGENT: PATH service_id - Actualizando estado con service_id: {tool_output.service_id}")
            
            result_data = {
                "service_id": tool_output.service_id,
                "messages": current_messages
            }
            if tool_output.requires_assessment is not None:
                result_data["requires_assessment"] = tool_output.requires_assessment
            if tool_output.service_name:
                result_data["service_name"] = tool_output.service_name
                print(f"📋 KNOWLEDGE AGENT: PATH service_id - Actualizando estado con service_name: {tool_output.service_name}")
            
            print(f"📋 KNOWLEDGE AGENT: PATH service_id - result_data completo: {result_data}")
            
            # Si estamos en modo resolución, regresar directamente al AppointmentAgent
            if is_service_resolution:
                print(f"📋 KNOWLEDGE AGENT: Modo resolución - regresando a AppointmentAgent con service_id")
                return Command(
                    update=result_data,
                    goto="AppointmentAgent"
                )
            else:
                # Retornar al supervisor para que decida si ir a AppointmentAgent
                return Command(
                    update=result_data,
                    goto="Supervisor"
                )
        elif tool_output.information_found:
            # Si se encontró información general (archivos o servicios), devolverla directamente y terminar
            from langchain_core.messages import AIMessage
            ai_message = AIMessage(content=tool_output.information_found, name="KnowledgeAgent")
            
            # IMPORTANTE: Si es información de un servicio, también actualizar el service_id en el estado
            update_data = {"messages": current_messages + [ai_message]}
            if tool_output.service_id:
                update_data["service_id"] = tool_output.service_id
                print(f"📋 KNOWLEDGE AGENT: Actualizando estado con service_id: {tool_output.service_id}")
                print(f"📋 KNOWLEDGE AGENT: update_data completo: {update_data}")
            if tool_output.service_name:
                update_data["service_name"] = tool_output.service_name
                print(f"📋 KNOWLEDGE AGENT: Actualizando estado con service_name: {tool_output.service_name}")
            if tool_output.requires_assessment is not None:
                update_data["requires_assessment"] = tool_output.requires_assessment
            
            return Command(
                update=update_data,
                goto="__end__"
            )
    
    elif isinstance(tool_output, UserFactsResult):
        if tool_output.facts_found:
            facts_text = "\n".join([f"- {fact}" for fact in tool_output.facts_found])
            response_message = f"{tool_output.summary}\n\n{facts_text}"
            from langchain_core.messages import AIMessage
            ai_message = AIMessage(content=response_message, name="KnowledgeAgent")
            return Command(
                update={"messages": current_messages + [ai_message]},
                goto="__end__"
            )
        else:
            # Si no se encuentran hechos, regresar al supervisor para que decida
            return Command(goto="Supervisor")
    
    elif isinstance(tool_output, UserSessionsResult):
        if tool_output.conversations_found:
            conversations_text = "\n".join([f"- {conv}" for conv in tool_output.conversations_found])
            response_message = f"{tool_output.summary}\n\n{conversations_text}"
            from langchain_core.messages import AIMessage
            ai_message = AIMessage(content=response_message, name="KnowledgeAgent")
            return Command(
                update={"messages": current_messages + [ai_message]},
                goto="__end__"
            )
        else:
            return Command(goto="Supervisor")

    elif isinstance(tool_output, UserInsightsResult):
        if tool_output.insights_found:
            insights_text = "\n".join([f"- {insight}" for insight in tool_output.insights_found])
            response_message = f"{tool_output.summary}\n\n{insights_text}"
            from langchain_core.messages import AIMessage
            ai_message = AIMessage(content=response_message, name="KnowledgeAgent")
            return Command(
                update={"messages": current_messages + [ai_message]},
                goto="__end__"
            )
        else:
            return Command(goto="Supervisor")

    # Si el resultado es un string directo (respuesta del LLM)
    elif isinstance(tool_output, str):
        from langchain_core.messages import AIMessage
        ai_message = AIMessage(content=tool_output, name="KnowledgeAgent")
        return Command(
            update={"messages": current_messages + [ai_message]},
            goto="__end__"
        )

    # Caso por defecto o si la salida no es lo que esperamos (ej. búsqueda vacía)
    print("⚠️ El Knowledge Agent no produjo una salida clara. Terminando para evitar bucle.")
    from langchain_core.messages import AIMessage
    ai_message = AIMessage(content="No estoy seguro de cómo proceder. ¿Puedes reformular tu pregunta? Si lo prefieres, puedo contactar a un asesor.", name="KnowledgeAgent")
    return Command(
        update={"messages": current_messages + [ai_message]},
        goto="__end__"
    )