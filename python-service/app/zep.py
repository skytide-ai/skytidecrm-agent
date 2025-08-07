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
        # No bloquear arranque si falta; usar mock ligero que cumple interfaz mínima
        print("⚠️ ZEP_API_KEY no configurada. Ejecutando sin memoria de Zep.")
        class _NoopZep:
            async def thread(self, *args, **kwargs):
                raise AttributeError
        # Devolvemos un cliente AsyncZep con API key vacía no es válido; devolvemos un objeto mínimo
        return None
    
    try:
        return AsyncZep(api_key=zep_api_key)
    except Exception as e:
        print(f"Error creating Zep Cloud client: {e}")
        raise

# Cliente global de Zep (puede ser None si no hay API key)
zep_client = get_zep_client()

# --- Funciones de Memoria (NUEVA ESTRUCTURA) ---

async def get_zep_context_block(thread_id: str, mode: str = "basic") -> Optional[str]:
    """
    Obtiene el 'Context Block' de Zep, que es un resumen optimizado para LLMs.
    
    Args:
        thread_id: ID del thread.
        mode: 'basic' (rápido, crudo) o 'summary' (lento, resumido).
    """
    try:
        print(f"Buscando 'Context Block' de Zep para thread_id: {thread_id} (modo: {mode})")
        if not zep_client:
            return None
        context_response = await zep_client.thread.get_user_context(thread_id=thread_id, mode=mode)
        
        if context_response and hasattr(context_response, 'context'):
            print(f"✅ 'Context Block' de Zep encontrado.")
            return context_response.context
        else:
            print("🤷 No se encontró 'Context Block' en Zep.")
            return None
    except Exception as e:
        print(f"❌ Error obteniendo 'Context Block' de Zep thread {thread_id}: {e}")
        return None

async def get_zep_last_messages(thread_id: str, last_n: int = 6) -> List[Message]:
    """
    Obtiene los últimos N mensajes de un thread de Zep para memoria a corto plazo.
    
    Args:
        thread_id: ID del thread.
        last_n: Número de mensajes a obtener.
    """
    try:
        print(f"Buscando los últimos {last_n} mensajes para thread_id: {thread_id}")
        if not zep_client:
            return []
        thread_data = await zep_client.thread.get(thread_id)
        
        if thread_data and hasattr(thread_data, 'messages') and thread_data.messages:
            last_messages = thread_data.messages[-last_n:]
            print(f"✅ Memoria a corto plazo encontrada: {len(last_messages)} mensajes.")
            return last_messages
        else:
            print("🤷 No se encontraron mensajes en la memoria de Zep.")
            return []
    except Exception as e:
        print(f"❌ Error obteniendo últimos mensajes de Zep thread {thread_id}: {e}")
        return []

# --- Funciones Auxiliares (Existentes) ---

async def ensure_user_exists(user_id: str, first_name: str = "", last_name: str = "", email: str = "") -> bool:
    """
    Asegura que un usuario existe en Zep.
    """
    try:
        await zep_client.user.add(
            user_id=user_id,
            first_name=first_name,
            last_name=last_name,
            email=email
        )
        print(f"✅ Usuario Zep creado: {user_id}")
        return True
    except Exception as e:
        error_str = str(e).lower()
        if "already exists" in error_str or "user already exists" in error_str:
            print(f"✅ Usuario Zep ya existe: {user_id}")
            return True
        else:
            print(f"❌ Error procesando usuario Zep {user_id}: {e}")
            return False

async def ensure_thread_exists(thread_id: str, user_id: str) -> bool:
    """
    Asegura que un thread existe en Zep.
    """
    try:
        await zep_client.thread.create(
            thread_id=thread_id,
            user_id=user_id
        )
        print(f"✅ Thread Zep creado: {thread_id} para usuario {user_id}")
        return True
    except Exception as e:
        error_str = str(e).lower()
        if "already exists" in error_str or "session with id" in error_str:
            print(f"✅ Thread Zep ya existe: {thread_id}")
            return True
        else:
            print(f"❌ Error creando thread Zep {thread_id}: {e}")
            return False

async def add_messages_to_zep(thread_id: str, messages: List[Message]) -> Dict[str, Any]:
    """
    Agrega mensajes a un thread de Zep.
    """
    try:
        if not zep_client:
            return {"success": True}
        await zep_client.thread.add_messages(
            thread_id=thread_id, 
            messages=messages
        )
        return {"success": True}
    except Exception as e:
        print(f"❌ Error agregando mensajes a Zep thread {thread_id}: {e}")
        return {"success": False, "error": str(e)}

async def update_zep_user_with_real_data(user_id: str, first_name: str, last_name: str) -> bool:
    """
    Actualiza un usuario de Zep con datos reales.
    """
    try:
        await zep_client.user.update(
            user_id=user_id,
            first_name=first_name,
            last_name=last_name
        )
        print(f"✅ Usuario Zep actualizado con datos reales: {user_id} -> {first_name} {last_name}")
        return True
    except Exception as e:
        print(f"❌ Error actualizando usuario Zep {user_id}: {e}")
        return False

# NOTA: Se eliminaron las funciones de búsqueda de facts, nodes, etc. que no se estaban usando
# para mantener el archivo limpio. Si se necesitan en el futuro, se pueden re-implementar.
