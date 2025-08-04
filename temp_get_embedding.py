import openai
import os
import json
from dotenv import load_dotenv

# Carga las variables de entorno desde el .env del servicio de Python
load_dotenv(dotenv_path='python-service/.env')

# Configura el cliente de OpenAI
client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

def generate_embedding(text: str):
    """Genera un embedding para un texto dado."""
    try:
        response = client.embeddings.create(
            model="text-embedding-3-small",
            input=text
        )
        return response.data[0].embedding
    except Exception as e:
        print(f"Error al generar embedding: {e}")
        return None

# Genera el embedding para la palabra "ubicacion" y lo imprime como un string JSON
embedding_vector = generate_embedding("ubicacion")
if embedding_vector:
    print(json.dumps(embedding_vector))
