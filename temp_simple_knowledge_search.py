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