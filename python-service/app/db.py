import os
from supabase import create_client, Client
from dotenv import load_dotenv

# Cargar variables de entorno desde el archivo .env
load_dotenv()

def get_supabase_client() -> Client:
    """
    Crea y devuelve un cliente de Supabase utilizando las credenciales
    de las variables de entorno.
    """
    url: str = os.environ.get("SUPABASE_URL")
    key: str = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

    if not url or not key:
        raise ValueError("Supabase URL and Key must be set in environment variables.")

    try:
        supabase_client: Client = create_client(url, key)
        print("Supabase client created successfully.")
        return supabase_client
    except Exception as e:
        print(f"Error creating Supabase client: {e}")
        raise

# Crear una instancia global del cliente para ser usada en la aplicación.
# Esto sigue el patrón de tener una única instancia a lo largo del ciclo de vida de la app.
supabase_client = get_supabase_client() 