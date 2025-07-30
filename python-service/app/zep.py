import os
from typing import Optional, Dict, Any, Tuple, List
from zep_python import ZepClient, Memory, Message
from dotenv import load_dotenv

load_dotenv()

# --- Configuración del Cliente Zep ---

def get_zep_client() -> ZepClient:
    """
    Crea y devuelve un cliente de Zep configurado para Zep Cloud.
    """
    zep_api_url = os.environ.get("ZEP_API_URL")
    zep_api_key = os.environ.get("ZEP_API_KEY")

    if not zep_api_url or not zep_api_key:
        print("Error: ZEP_API_URL and ZEP_API_KEY must be set for Zep Cloud.")
        raise ValueError("Zep Cloud API URL and Key are not configured.")
    
    try:
        return ZepClient(base_url=zep_api_url, api_key=zep_api_key)
    except Exception as e:
        print(f"Error creating Zep Cloud client: {e}")
        raise

zep_client = get_zep_client()

# --- Funciones para la Gestión de Memoria y Estado ---
# La gestión del estado ahora es manejada por el checkpointer de LangGraph.
# Esta sección se mantiene por si se necesitan futuras funciones relacionadas con Zep,
# pero la carga/guardado de estado manual ha sido eliminada. 