import os
import uuid
from typing import Optional, Dict, Any, List
from zep_cloud.client import AsyncZep
from zep_cloud import Message
from dotenv import load_dotenv

load_dotenv()

# --- Configuración del Cliente Zep Cloud ---

def get_zep_client() -> AsyncZep:
    """
    Crea y devuelve un cliente de Zep Cloud configurado correctamente.
    """
    zep_api_key = os.environ.get("ZEP_API_KEY")

    if not zep_api_key:
        print("Error: ZEP_API_KEY must be set for Zep Cloud.")
        raise ValueError("Zep Cloud API Key is not configured.")
    
    try:
        return AsyncZep(api_key=zep_api_key)
    except Exception as e:
        print(f"Error creating Zep Cloud client: {e}")
        raise

# Cliente global de Zep
zep_client = get_zep_client()

# --- Funciones Auxiliares para Zep Cloud ---

async def ensure_user_exists(user_id: str, first_name: str = "", last_name: str = "", email: str = "") -> bool:
    """
    Asegura que un usuario existe en Zep, usando la API oficial.
    En Zep Cloud, si el usuario no existe, se crea automáticamente.
    """
    try:
        # Crear o actualizar usuario usando la API oficial de Zep Cloud
        await zep_client.user.add(
            user_id=user_id,
            first_name=first_name,
            last_name=last_name,
            email=email
        )
        print(f"✅ Usuario Zep creado: {user_id}")
        return True
    except Exception as e:
        # Verificar si el error es porque el usuario ya existe (caso normal)
        error_str = str(e).lower()
        if "already exists" in error_str or "user already exists" in error_str:
            print(f"✅ Usuario Zep ya existe: {user_id}")
            return True
        else:
            print(f"❌ Error procesando usuario Zep {user_id}: {e}")
            return False

async def ensure_thread_exists(thread_id: str, user_id: str) -> bool:
    """
    Asegura que un thread existe en Zep, usando la API oficial.
    """
    try:
        # Crear thread usando la API oficial de Zep Cloud
        await zep_client.thread.create(
            thread_id=thread_id,
            user_id=user_id
        )
        print(f"✅ Thread Zep creado: {thread_id} para usuario {user_id}")
        return True
    except Exception as e:
        # Verificar si el error es porque el thread ya existe (caso normal)
        error_str = str(e).lower()
        if "already exists" in error_str or "session with id" in error_str:
            print(f"✅ Thread Zep ya existe: {thread_id}")
            return True
        else:
            print(f"❌ Error creando thread Zep {thread_id}: {e}")
            return False

async def add_messages_to_zep(thread_id: str, messages: List[Message], return_context: bool = False) -> Dict[str, Any]:
    """
    Agrega mensajes a un thread de Zep usando la API oficial.
    
    Args:
        thread_id: ID del thread
        messages: Lista de mensajes a agregar
        return_context: Si True, retorna el contexto inmediatamente sin llamada adicional
    
    Returns:
        Dict con 'success' y opcionalmente 'context' si return_context=True
    """
    try:
        # API oficial de Zep Cloud para agregar mensajes a un thread
        await zep_client.thread.add_messages(
            thread_id=thread_id, 
            messages=messages
        )
        
        result = {"success": True}
        if return_context:
            # Si se solicita contexto, usar la API oficial get_user_context
            context_response = await zep_client.thread.get_user_context(thread_id=thread_id)
            if context_response and hasattr(context_response, 'context'):
                result["context"] = context_response.context
            
        return result
    except Exception as e:
        print(f"❌ Error agregando mensajes a Zep thread {thread_id}: {e}")
        return {"success": False, "error": str(e)}

async def get_zep_memory_context(thread_id: str, min_rating: float = 0.0, mode: str = "basic") -> str:
    """
    Obtiene el contexto de memoria relevante desde Zep usando la API oficial.
    
    Args:
        thread_id: ID del thread  
        min_rating: Rating mínimo para filtrar contexto (no usado en API actual)
        mode: "basic" para mejor performance (no usado en API actual)
    """
    try:
        # Usar la API oficial de Zep Cloud para obtener contexto del thread
        memory = await zep_client.thread.get_user_context(thread_id=thread_id)
        return memory.context if memory and memory.context else ""
    except Exception as e:
        print(f"❌ Error obteniendo contexto de Zep thread {thread_id}: {e}")
        return ""

async def search_zep_facts(user_id: str, query: str, limit: int = 5) -> List[str]:
    """
    Busca hechos relevantes en las conversaciones de un usuario usando la API oficial.
    """
    try:
        # Usar la API oficial de Zep Cloud para buscar edges/facts
        result = await zep_client.graph.search(
            user_id=user_id,
            query=query,
            limit=limit,
            scope="edges"  # API oficial usa 'scope' en lugar de 'search_scope'
        )
        facts = []
        if hasattr(result, 'edges') and result.edges:
            for edge in result.edges:
                if hasattr(edge, 'fact') and edge.fact:
                    facts.append(edge.fact)
        return facts
    except Exception as e:
        print(f"❌ Error buscando facts en Zep para {user_id}: {e}")
        return []

async def search_zep_nodes(user_id: str, query: str, limit: int = 5) -> List[str]:
    """
    Busca nodos/entidades relevantes en el grafo de conocimiento usando la API oficial.
    """
    try:
        # Usar la API oficial de Zep Cloud para buscar nodes
        result = await zep_client.graph.search(
            user_id=user_id,
            query=query,
            limit=limit,
            scope="nodes"  # API oficial usa 'scope' en lugar de 'search_scope'
        )
        nodes = []
        if hasattr(result, 'nodes') and result.nodes:
            for node in result.nodes:
                if hasattr(node, 'summary') and node.summary:
                    nodes.append(node.summary)
        return nodes
    except Exception as e:
        print(f"❌ Error buscando nodes en Zep para {user_id}: {e}")
        return []

async def search_zep_sessions(user_id: str, query: str, limit: int = 5) -> List[str]:
    """
    Busca edges relacionados con sesiones/conversaciones usando graph.search.
    """
    try:
        # Usar graph.search para buscar información de conversaciones
        result = await zep_client.graph.search(
            user_id=user_id,
            query=query,
            limit=limit,
            scope="edges"  # Buscar en edges que contengan información de conversaciones
        )
        conversations = []
        if hasattr(result, 'edges') and result.edges:
            for edge in result.edges:
                if hasattr(edge, 'fact') and edge.fact:
                    conversations.append(edge.fact)
        return conversations
    except Exception as e:
        print(f"❌ Error buscando conversaciones en Zep para {user_id}: {e}")
        return []

async def update_zep_user_with_real_data(user_id: str, first_name: str, last_name: str) -> bool:
    """
    Actualiza un usuario de Zep con datos reales obtenidos durante el booking.
    
    Args:
        user_id: ID del usuario en Zep (formato: chat_{chatIdentityId})
        first_name: Nombre real del usuario
        last_name: Apellido real del usuario
    
    Returns:
        bool: True si la actualización fue exitosa
    """
    try:
        await zep_client.user.update(
            user_id=user_id,
            first_name=first_name,
            last_name=last_name
            # Intencionalmente omitimos email según requerimiento
        )
        print(f"✅ Usuario Zep actualizado con datos reales: {user_id} -> {first_name} {last_name}")
        return True
    except Exception as e:
        print(f"❌ Error actualizando usuario Zep {user_id}: {e}")
        return False



# --- Configuración de Variables de Entorno ---
# NOTA: Con Zep Cloud, ya no necesitamos ZEP_API_URL, solo ZEP_API_KEY 