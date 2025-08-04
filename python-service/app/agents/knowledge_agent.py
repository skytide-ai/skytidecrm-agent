from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field, field_validator
from uuid import UUID
import pydantic_ai
from openai import AsyncOpenAI
from pydantic_ai import Agent, RunContext
from pydantic_ai.tools import ToolDefinition
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
    """Modelo para el resultado de la búsqueda de conocimiento con validación estricta de tipos."""
    # Para servicios - UUID validado
    service_id: Optional[UUID] = Field(default=None, description="El ID único del servicio encontrado (debe ser UUID válido).")
    service_name: Optional[str] = Field(default=None, min_length=1, description="Nombre del servicio encontrado.")
    requires_assessment: Optional[bool] = Field(default=None, description="Si el servicio requiere valoración previa.")
    
    # Para información general (archivos)
    information_found: Optional[str] = Field(default=None, min_length=1, description="Información específica encontrada (ubicación, horarios, contacto, etc.)")
    source_type: Optional[str] = Field(default=None, description="Tipo de fuente: 'service' o 'file'")
    category: Optional[str] = Field(default=None, description="Categoría del archivo si es de tipo 'file'")
    
    # Para casos especiales
    clarification_message: Optional[str] = Field(default=None, min_length=1, description="Mensaje para pedir aclaración al usuario si la búsqueda es ambigua o no arroja resultados.")
    
    @field_validator('source_type')
    @classmethod
    def validate_source_type(cls, v):
        if v is not None and v not in ['service', 'file', 'multiple_services']:
            raise ValueError("source_type debe ser 'service', 'file' o 'multiple_services'")
        return v
    
    @field_validator('service_name', 'information_found', 'clarification_message')
    @classmethod
    def validate_non_empty_strings(cls, v):
        if v is not None and (not isinstance(v, str) or len(v.strip()) == 0):
            raise ValueError("El texto no puede estar vacío")
        return v.strip() if isinstance(v, str) else v



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

# --- Función para control inteligente de herramientas ---
async def smart_tool_preparation(ctx: RunContext[GlobalState], tool_defs: List[ToolDefinition]) -> List[ToolDefinition]:
    """
    Función prepare_tools que controla dinámicamente qué herramientas usar según el contexto.
    Implementa análisis semántico sin hardcodeo de palabras específicas.
    """
    if not tool_defs:
        print("⚠️ WARNING: No hay herramientas disponibles para el agente")
        return []
    
    state = ctx.deps
    
    # Obtener el último mensaje del usuario
    last_message = state.get("messages", [])[-1] if state.get("messages") else None
    user_query = last_message.content if last_message else ""
    
    # 🧠 ANÁLISIS INTELIGENTE DEL CONTEXTO (SIN HARDCODEO)
    available_tools = []
    
    # 1. DETECTAR CAMBIO DE SERVICIO: Análisis contextual
    has_existing_service = state.get('service_id') is not None
    # CRÍTICO: Solo si hay indicadores EXPLÍCITOS de cambio, no solo que haya un servicio
    explicit_change_keywords = ["mejor", "cambio", "prefiero", "en lugar de", "no quiero", "diferente", "otro"]
    is_likely_service_change = (
        has_existing_service and 
        len(user_query.split()) >= 2 and  # Más de una palabra (sugiere especificidad)
        user_query.strip() != "" and  # No es mensaje vacío
        any(keyword in user_query.lower() for keyword in explicit_change_keywords)  # Indicadores de cambio
    )
    
    # 2. DETECTAR NECESIDAD DE HISTORIAL: Análisis semántico
    # Si el usuario hace referencia a interacciones pasadas, usa pronombres demostrativos,
    # o palabras que sugieren historia personal
    query_suggests_history = (
        # Referencias temporales al pasado
        any(ref in user_query.lower() for ref in ["antes", "anterior", "previo", "ya", "otra vez"]) or
        # Referencias pronominales (el que, la que, ese, esta, etc.)
        any(ref in user_query.lower() for ref in ["el que", "la que", "ese", "esa", "esto", "eso", "aquel"]) or
        # Referencias a acciones previas del asistente
        any(ref in user_query.lower() for ref in ["dijiste", "mencionaste", "recomendaste", "sugeriste"]) or
        # Referencias a información personal (sin hardcodear condiciones médicas específicas)
        any(ref in user_query.lower() for ref in ["mi ", "mis ", "tengo ", "soy ", "problema"])
    )
    
    # 3. APLICAR LÓGICA DE HERRAMIENTAS
    for tool_def in tool_defs:
        # SIEMPRE incluir knowledge_search - es la herramienta principal
        if tool_def.name == 'knowledge_search':
            available_tools.append(tool_def)
        
        # Herramientas de historial: solo si realmente se necesitan
        elif tool_def.name in ['search_user_facts', 'search_user_conversations', 'search_user_insights']:
            # REGLA: Solo incluir si hay evidencia semántica de necesidad de historial
            # Y NO es un cambio directo de servicio
            if query_suggests_history and not is_likely_service_change:
                available_tools.append(tool_def)
    
    # 4. LOGGING INTELIGENTE
    context_mode = "GENÉRICO"
    if is_likely_service_change:
        context_mode = "CAMBIO_SERVICIO"
    elif query_suggests_history:
        context_mode = "NECESITA_HISTORIAL"
    
    print(f"🔧 TOOLS PREPARADOS: {len(available_tools)} herramientas | Modo: {context_mode}")
    print(f"🧠 Herramientas disponibles: {[t.name for t in available_tools]}")
    
    return available_tools

# --- Definición del Agente de Conocimiento ---
knowledge_agent = Agent[GlobalState, KnowledgeSearchResult](
    'openai:gpt-4o', 
    deps_type=GlobalState,
    output_type=KnowledgeSearchResult,  # ← FUERZA que el agente SIEMPRE devuelva KnowledgeSearchResult
    prepare_tools=smart_tool_preparation,
    system_prompt="""
    🧠 ASISTENTE INTELIGENTE CON ANÁLISIS CONTEXTUAL

    Eres un asistente experto que entiende las intenciones del usuario y responde apropiadamente.
    
    **REGLA CRÍTICA: MÁXIMO UNA BÚSQUEDA POR CONSULTA**
    - Usa knowledge_search UNA SOLA VEZ por consulta del usuario
    - Si la primera búsqueda no encuentra información relevante, NO hagas más búsquedas
    - Si no encuentras información específica, ofrece conectar con un asesor
    
    **TU MISIÓN:**
    - INFORMAR cuando el usuario solo quiere conocer sobre servicios
    - PREPARAR PARA AGENDAR cuando el usuario tiene intención clara de reservar
    
    **ANÁLISIS DE INTENCIONES:**
    1. **CONSULTA INFORMATIVA**: Solo quiere saber → Proporciona información, NO guardes service_id
    2. **INTENCIÓN DE AGENDAR**: Quiere reservar → Busca servicio Y guarda service_id para siguiente paso

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
    
    🎯 CASO ESPECIAL - CAMBIO DE SERVICIO PARA AGENDAR:
    Si el usuario dice "quiero agendar [servicio]" o "mejor [servicio]" o "cambio a [servicio]":
    - SOLO haz 1 búsqueda: knowledge_search con el nombre del servicio
    - OBJETIVO: Encontrar el service_id únicamente
    - NO busques horarios, disponibilidad, ni hagas múltiples consultas
    - Una vez que tengas el service_id, TERMINA
    
    FLUJO NORMAL:
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
    - MÁXIMO UNA BÚSQUEDA: Si knowledge_search no encuentra información relevante, NO busques de nuevo
    - Si no encuentras información específica, explica amablemente y ofrece conectar con asesor
    - SOLO usa datos reales retornados por las herramientas de búsqueda
    - NO hagas suposiciones sobre qué servicios podrían existir
    - NO reformules la búsqueda con sinónimos si la primera no fue exitosa
    
    📝 FORMATO DE RESPUESTAS:
    - Habla de manera natural y conversacional, evita listas técnicas
    - Usa un tono amigable como "Te cuento que...", "Mira, tenemos...", "Perfecto, aquí está..."
    - Organiza la información de manera fluida, no como puntos de lista
    - Cuando presentes múltiples servicios, hazlo como si estuvieras recomendando personalmente
    - USA EMOJIS para hacer la conversación más natural y amigable 😊
    - NO uses asteriscos (**texto**) ni formato markdown para títulos
    - Escribe los nombres de servicios de forma natural, como en una conversación normal
    
    🎯 OBJETIVO: Ayudar al usuario encontrando información relevante usando las herramientas necesarias.
    
    🧠 SÉ INTELIGENTE CON LAS HERRAMIENTAS:
    - EVALÚA primero qué necesitas realmente buscar
    - NO uses todas las herramientas para cada consulta
    - Solo busca historial del usuario si es RELEVANTE para la consulta
    
    GUÍA DE CUÁNDO USAR CADA HERRAMIENTA:
    - 'knowledge_search': SIEMPRE para encontrar servicios, ubicación, horarios, precios, info general
    - 'search_user_facts': SOLO si el usuario menciona "antes", "preferencias", o necesitas personalizar
    - 'search_user_conversations': SOLO si el usuario pregunta sobre conversaciones pasadas
    - 'search_user_insights': SOLO si necesitas patrones de comportamiento para recomendar
    
    ✅ EJEMPLOS DE USO EFICIENTE:
    - "Quiero agendar una cita" → SOLO knowledge_search (para encontrar servicio)
    - "¿Dónde quedan?" → SOLO knowledge_search (info general)
    - "¿Qué me recomendaste antes?" → search_user_conversations + knowledge_search
    - "Mis servicios favoritos" → search_user_facts + knowledge_search
    
    Siempre proporciona información útil y amigable en tu respuesta final.
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
            'servicios', 'qué tienen', 'qué ofrecen', 'catálogo', 'productos',
            'ofertas', 'que hacen', 'especialidades', 'opciones', 'disponible'
        ])
        
        # Analizar el tipo de resultado y consolidar información si es necesario
        best_result = matching_results[0]
        metadata = best_result.get('metadata', {})
        source_type = metadata.get('source_type')
        similarity = best_result.get('similarity', 0)
        
        print(f"✅ Información encontrada: source_type={source_type}, similarity={similarity:.2f}")
        print(f"🔍 Búsqueda amplia de servicios detectada: {is_broad_service_query}")
        
        # 🎯 UMBRAL DE SIMILARITY: Si la similarity es muy baja, no es información útil
        if similarity < 0.1:
            return KnowledgeSearchResult(
                clarification_message=f"No encontré información específica sobre '{query}'. ¿Te gustaría que te conecte con un asesor para obtener información más detallada? 😊"
            )
        
        # 🧠 ANÁLISIS SEMÁNTICO INTELIGENTE: ¿La información encontrada realmente responde a la pregunta?
        query_keywords = query.lower()
        content_preview = best_result.get('content', '').lower()
        
        # Para consultas específicas, verificar que el contenido realmente contenga información relevante
        specific_queries = {
            'promocion': ['promocion', 'descuento', 'oferta', 'especial', '% off', 'rebaja'],
            'precio': ['precio', 'costo', 'valor', '$', 'pesos', 'tarifa'],
            'horario': ['horario', 'hora', 'atencion', 'abierto', 'cerrado', 'lunes', 'martes'],
            'ubicacion': ['ubicacion', 'direccion', 'donde', 'lugar', 'encontrar'],
            'contacto': ['telefono', 'celular', 'whatsapp', 'contacto', 'comunicar']
        }
        
        # Detectar si es una consulta específica
        specific_query_detected = None
        for query_type, keywords in specific_queries.items():
            if any(keyword in query_keywords for keyword in keywords):
                specific_query_detected = query_type
                break
        
        # Si es una consulta específica, verificar que el contenido la responda
        if specific_query_detected:
            relevant_keywords = specific_queries[specific_query_detected]
            content_is_relevant = any(keyword in content_preview for keyword in relevant_keywords)
            
            # Si el contenido NO es relevante para la consulta específica
            if not content_is_relevant and similarity < 0.6:  # Umbral más alto para consultas específicas
                print(f"🚫 CONTENIDO NO RELEVANTE: Consulta sobre '{specific_query_detected}' pero contenido no contiene información relevante")
                return KnowledgeSearchResult(
                    clarification_message=f"No encontré información específica sobre {query} en nuestra base de datos. ¿Te gustaría que te conecte con un asesor que pueda ayudarte con esta consulta? 😊"
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
                suggestion = "\n\n💡 Este es uno de nuestros servicios. Si buscas algo específico o quieres conocer otros servicios disponibles, puedes preguntarme por el tipo de servicio que te interesa 😊"
                
                return KnowledgeSearchResult(
                    information_found=content + suggestion,
                    source_type='service',
                    service_id=UUID(list(unique_services.keys())[0])
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
                service_id=UUID(service_id)
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
    
    # ✅ CONSTRUIR HISTORIAL COMPLETO DE LA CONVERSACIÓN
    history_messages = state["messages"][:-1]  # Todos excepto el último (mensaje actual)
    history_str = "\n".join([f"{msg.__class__.__name__}: {msg.content}" for msg in history_messages])
    
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
    
    # Construir query enriquecido con contexto completo
    if is_service_resolution and zep_context:
        # Cuando venimos del appointment_agent, extraer el servicio del contexto
        # Usar un prompt MUY específico que solo pida identificación del servicio
        enhanced_query = f"IDENTIFICAR SERVICIO: Encuentra únicamente el service_id del servicio mencionado en: {zep_context}. SOLO usa knowledge_search y retorna el resultado estructurado. NO generes texto conversacional."
        print(f"🔍 KNOWLEDGE AGENT: Query de resolución: {enhanced_query}")
    else:
        # 🧠 DETECCIÓN INTELIGENTE DE CAMBIO DE SERVICIO (SIN HARDCODEO)
        
        # CONTEXTO: ¿Ya hay un servicio seleccionado?
        has_existing_service = state.get('service_id') is not None
        
        # ANÁLISIS SEMÁNTICO: ¿El mensaje sugiere un cambio REAL de servicio?
        # Solo debe activarse si realmente es un cambio, no la primera búsqueda
        is_service_change_request = (
            has_existing_service and  # Ya hay un servicio en contexto
            len(user_query.split()) >= 2 and  # Mensaje con suficiente contenido
            user_query.strip() != "" and  # No está vacío
            # CRÍTICO: Solo si hay indicadores EXPLÍCITOS de cambio
            any(change_indicator in user_query.lower() for change_indicator in [
                "mejor", "prefiero", "cambio", "en lugar de", "no quiero", "diferente", "otro"
            ])
        )
        
        if is_service_change_request:
            # Prompt específico para cambio de servicio - INTELIGENTE
            enhanced_query = f"""🎯 ANÁLISIS DE CAMBIO DE CONTEXTO

ESTADO ACTUAL: Ya hay un servicio seleccionado (ID: {state.get('service_id', 'N/A')})

HISTORIAL PREVIO:
{history_str}

NUEVA SOLICITUD: {user_query}

ANÁLISIS REQUERIDO:
- Analiza si el usuario quiere cambiar a un servicio diferente
- Si es así, identifica el nuevo servicio mencionado en su solicitud
- Usa SOLO knowledge_search UNA VEZ para encontrar el service_id del nuevo servicio
- Tu única misión: encontrar el service_id del servicio solicitado
- NO busques información adicional como horarios o disponibilidad
- NO hagas múltiples búsquedas
- Una vez encontrado el service_id, tu tarea está completa

IMPORTANTE: Confía en tu análisis semántico del contexto, no busques palabras específicas."""
        else:
            # ✅ FLUJO NORMAL CON HISTORIAL COMPLETO
            enhanced_query = f"""HISTORIAL DE LA CONVERSACIÓN:
{history_str}

CONSULTA ACTUAL DEL USUARIO: {user_query}

IMPORTANTE: Si en el historial mencioné servicios específicos y el usuario se refiere a ellos (ej: "el primero", "ese", "el que dijiste"), identifica a qué servicio se refiere basándote en mis respuestas anteriores."""
        
        if zep_context:
            enhanced_query = f"Contexto del usuario (memoria): {zep_context}\n\n{enhanced_query}"
    
    # FORZAR EL USO DE HERRAMIENTAS - Nunca responder directamente
    # En Pydantic AI, forzamos el uso de herramientas via prepare_tools y validación
    result = await knowledge_agent.run(enhanced_query, deps=state)
    
    # El resultado del agente es GARANTIZADO de ser KnowledgeSearchResult gracias a output_type
    tool_output = result.output
    print(f"✅ STRUCTURED OUTPUT: {type(tool_output)}")
    print(f"🔍 DEBUG KnowledgeSearchResult:")
    print(f"🔍 service_id: {tool_output.service_id}")
    print(f"🔍 service_name: {tool_output.service_name}")
    print(f"🔍 information_found: {bool(tool_output.information_found)}")
    print(f"🔍 clarification_message: {bool(tool_output.clarification_message)}")

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
            # 🧠 ANÁLISIS INTELIGENTE: ¿El usuario quiere agendar o solo informarse?
            user_wants_to_book = any(keyword in user_query.lower() for keyword in [
                "agendar", "reservar", "programar", "cita", "gustaría agendar", "quiero agendar", "puedo agendar"
            ])
            
            if user_wants_to_book or is_service_resolution:
                # CASO 1: USUARIO QUIERE AGENDAR → Guardar service_id en estado
                print(f"🎯 KNOWLEDGE AGENT: Usuario quiere agendar → Guardando service_id: {tool_output.service_id}")
                
                result_data = {
                    "service_id": str(tool_output.service_id),
                    "messages": current_messages
                }
                if tool_output.requires_assessment is not None:
                    result_data["requires_assessment"] = tool_output.requires_assessment
                if tool_output.service_name:
                    result_data["service_name"] = tool_output.service_name
                    print(f"📋 KNOWLEDGE AGENT: Guardando service_name: {tool_output.service_name}")
                
                # Si estamos en modo resolución, ir directo a AppointmentAgent
                if is_service_resolution:
                    print(f"📋 KNOWLEDGE AGENT: Modo resolución → AppointmentAgent")
                    return Command(update=result_data, goto="AppointmentAgent")
                else:
                    # Retornar al supervisor para continuar flujo de agendamiento
                    print(f"📋 KNOWLEDGE AGENT: Preparado para agendar → Supervisor")
                    return Command(update=result_data, goto="Supervisor")
                    
            else:
                # CASO 2: SOLO CONSULTA INFORMATIVA → NO guardar service_id, solo responder
                print(f"💡 KNOWLEDGE AGENT: Solo consulta informativa → Respondiendo sin guardar estado")
                
                if tool_output.information_found:
                    from langchain_core.messages import AIMessage
                    ai_message = AIMessage(content=tool_output.information_found, name="KnowledgeAgent")
                    return Command(
                        update={"messages": current_messages + [ai_message]},
                        goto="__end__"
                    )
                else:
                    # Si por alguna razón no hay información, ir al supervisor
                    return Command(goto="Supervisor")
        elif tool_output.information_found:
            # Si se encontró información general (archivos o servicios), devolverla directamente y terminar
            from langchain_core.messages import AIMessage
            ai_message = AIMessage(content=tool_output.information_found, name="KnowledgeAgent")
            
            # IMPORTANTE: Si es información de un servicio, también actualizar el service_id en el estado
            update_data = {"messages": current_messages + [ai_message]}
            if tool_output.service_id:
                update_data["service_id"] = str(tool_output.service_id)  # Convertir UUID a string para el estado
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