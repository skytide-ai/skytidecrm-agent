from typing import Dict, Any
import httpx
import os

# Importamos el estado global y el cliente de Supabase
from ..state import GlobalState
from ..db import supabase_client

# --- Función de Lógica de Negocio ---
async def handle_human_escalation(organization_id: str, chat_identity_id: str, reason: str) -> Dict[str, Any]:
    """
    Marca una conversación para que sea atendida por un humano, desactiva el bot y envía una notificación.
    Este proceso sigue un orden estricto para garantizar que el bot solo se desactive si la notificación es exitosa.
    """
    print(f"--- ¡ESCALACIÓN HUMANA! ---")
    print(f"Chat ID: {chat_identity_id}")
    print(f"Contact ID: {contact_id}")
    print(f"Razón: {reason}")
    
    try:
        # 1. Obtener información del chat_identity (siempre existe)
        print("Obteniendo información del chat_identity...")
        chat_response = await supabase_client.table('chat_identities').select(
            'platform_user_id, contact_id'
        ).eq('id', chat_identity_id).single().execute()
        
        if not chat_response.data:
            raise Exception(f"No se pudo encontrar el chat_identity con ID {chat_identity_id}")
        
        platform_user_id = chat_response.data['platform_user_id']
        contact_id_from_chat = chat_response.data.get('contact_id')
        
        # El teléfono siempre viene del platform_user_id (sin el +)
        customer_phone = platform_user_id
        
        # 2. Intentar obtener el nombre real del contacto (si existe)
        customer_name = "Cliente"  # Nombre por defecto
        
        if contact_id_from_chat:
            print(f"Buscando nombre del contacto con ID: {contact_id_from_chat}")
            contact_response = await supabase_client.table('contacts').select(
                'first_name, last_name'
            ).eq('id', contact_id_from_chat).eq('organization_id', organization_id).maybe_single().execute()
            
            if contact_response.data:
                first_name = contact_response.data['first_name']
                last_name = contact_response.data['last_name']
                customer_name = f"{first_name} {last_name}".strip()
                print(f"Nombre del cliente encontrado: {customer_name}")
            else:
                print("contact_id existe pero no se encontró el registro en contacts, usando nombre por defecto")
        else:
            print("No hay contact_id vinculado, usando nombre por defecto")
        
        print(f"Datos para notificación: customer_name='{customer_name}', customer_phone='{customer_phone}'")
        
        # 2. Obtener configuración de notificaciones de escalación
        print("Obteniendo configuración de notificaciones...")
        notification_response = await supabase_client.table('internal_notifications_config').select(
            'recipient_phone, country_code'
        ).eq('organization_id', organization_id).eq('is_active', True).maybe_single().execute()
        
        if not notification_response.data or not notification_response.data.get('recipient_phone'):
            print("⚠️ No se encontró configuración de notificaciones activa para la organización.")
            return {
                "escalation_successful": False, # La escalación no se completó porque no hay a quién notificar
                "escalation_message": "Lo siento, no pudimos procesar tu solicitud en este momento. Por favor, intenta más tarde."
            }
        
        # Construir el número completo: quitar el + del country_code y concatenar con recipient_phone
        country_code = notification_response.data.get('country_code', '+57').replace('+', '')
        recipient_phone_local = notification_response.data['recipient_phone']
        recipient_phone = f"{country_code}{recipient_phone_local}"
        
        print(f"Número destinatario construido: {recipient_phone} (country_code: {country_code}, recipient_phone: {recipient_phone_local})")
        
        # 3. Enviar notificación a través del express-gateway (ANTES de desactivar el bot)
        print(f"Enviando notificación de escalación a {recipient_phone}...")
        gateway_url = os.getenv('EXPRESS_GATEWAY_URL', 'http://express-gateway:8080')
        notification_endpoint = f"{gateway_url}/internal/notify/escalation"
        
        notification_payload = {
            "organization_id": organization_id,
            "recipient_phone": recipient_phone,
            "customer_name": customer_name,
            "customer_phone": customer_phone,
            "escalation_reason": reason
        }
        
        notification_sent_successfully = False
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(notification_endpoint, json=notification_payload, timeout=10)
                if response.status_code == 200:
                    print("✅ Notificación de escalación enviada exitosamente al gateway.")
                    notification_sent_successfully = True
                else:
                    print(f"⚠️ Error al enviar notificación: {response.status_code} - {response.text}")
            except httpx.RequestError as exc:
                print(f"❌ Error de red al intentar contactar el gateway: {exc}")

        # 4. Desactivar el bot SOLO SI la notificación fue exitosa
        if notification_sent_successfully:
            print("Desactivando bot para este chat...")
            await supabase_client.table('chat_identities').update({
                'bot_enabled': False,
                'requires_human_intervention': True
            }).eq('id', chat_identity_id).execute()
            
            return {
                "escalation_successful": True,
                "escalation_message": "Un asesor se pondrá en contacto contigo en breve."
            }
        else:
            print("El bot NO será desactivado porque la notificación falló.")
            return {
                "escalation_successful": False,
                "escalation_message": "Lo siento, no pudimos procesar tu solicitud en este momento. Por favor, intenta más tarde."
            }
        
    except Exception as e:
        print(f"❌ Error crítico durante la escalación: {e}")
        return {
            "escalation_successful": False,
            "escalation_message": f"Hubo un error al procesar la escalación: {e}"
        }

# --- Función de Entrada (Entrypoint) para el Grafo ---
async def run_escalation_agent(state: GlobalState) -> Dict[str, Any]:
    """
    Punto de entrada para ejecutar el nodo de escalación.
    """
    print("--- Ejecutando Escalation Agent ---")
    
    # Validar que tenemos la información necesaria
    if not state.get('chat_identity_id'):
        print("❌ Error: No se encontró chat_identity_id en el estado")
        return {
            "messages": [("ai", "Lo siento, hubo un error interno. Por favor, intenta nuevamente.")],
            "next_agent": "terminate"
        }
    
    # Nota: Ya no requiere contact_id, se obtiene internamente desde chat_identity
    
    # La razón de la escalación podría venir del supervisor o de un análisis del sentimiento del usuario
    reason = "El usuario ha solicitado hablar con una persona."
    
    # Llamar a la función de escalación
    result = await handle_human_escalation(
        organization_id=state['organization_id'],
        chat_identity_id=state['chat_identity_id'],
        reason=reason
    )
    
    # Determinar el mensaje de respuesta basado en el resultado
    if result["escalation_successful"]:
        final_message = result["escalation_message"]
    else:
        final_message = "Lo siento, hubo un problema al procesar tu solicitud. Por favor, intenta nuevamente o contacta directamente a nuestro equipo."
    
    return {
        "messages": [("ai", final_message)],
        "next_agent": "terminate" # Le indicamos al supervisor que termine la ejecución del grafo.
    } 