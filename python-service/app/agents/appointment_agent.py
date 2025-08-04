from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta, date
from pydantic import BaseModel, Field, field_validator
from uuid import UUID
import pydantic_ai
from openai import OpenAI
import pytz
from pydantic_ai import Agent, RunContext

# Importamos el estado global, el cliente de Supabase y funciones de Zep
from ..state import GlobalState
from ..db import supabase_client
from ..zep import get_zep_memory_context

# --- Cliente Pydantic AI ---
client = OpenAI()

# --- Modelos de Datos para Herramientas con Validación ---
class AvailabilitySlot(BaseModel):
    """Slot de disponibilidad con validación de horarios y UUID."""
    start_time: str = Field(description="Hora de inicio en formato HH:MM")
    end_time: str = Field(description="Hora de fin en formato HH:MM")
    member_id: UUID = Field(description="ID único del miembro (UUID válido)")
    
    @field_validator('start_time', 'end_time')
    @classmethod
    def validate_time_format(cls, v):
        try:
            datetime.strptime(v, '%H:%M')
            return v
        except ValueError:
            raise ValueError("Formato de hora debe ser HH:MM")

class AppointmentConfirmation(BaseModel):
    """Confirmación de cita con validación de UUID opcional."""
    success: bool = Field(description="Si la cita fue creada exitosamente")
    appointment_id: Optional[UUID] = Field(default=None, description="ID único de la cita (UUID válido)")
    message: str = Field(min_length=1, description="Mensaje de confirmación para el usuario")
    opt_in_status: Optional[str] = Field(default="not_set", description="Estado de opt-in del usuario")
    
    @field_validator('opt_in_status')
    @classmethod
    def validate_opt_in_status(cls, v):
        if v not in ["opt_in", "opt_out", "not_set"]:
            raise ValueError("opt_in_status debe ser 'opt_in', 'opt_out' o 'not_set'")
        return v

class AppointmentInfo(BaseModel):
    """Información de cita con validación UUID."""
    appointment_id: UUID = Field(description="ID único de la cita (UUID válido)")
    summary: str = Field(min_length=1, description="Resumen de la información de la cita")

class ContactResolution(BaseModel):
    """Resultado de la resolución de contacto con validación."""
    success: bool = Field(description="Si la operación fue exitosa")
    contact_id: Optional[str] = Field(default=None, description="ID del contacto encontrado o creado")
    message: str = Field(min_length=1, description="Mensaje descriptivo del resultado")
    is_existing_contact: bool = Field(default=False, description="True si el contacto ya existía, False si se creó uno nuevo")

# --- Definición del Agente de Citas ---
appointment_agent = Agent[GlobalState](
    'openai:gpt-4o',
    deps_type=GlobalState,
    system_prompt="""
    Eres un asistente experto en la gestión de citas. Tu trabajo es guiar al usuario a través del proceso de agendamiento, consulta, cancelación y reprogramación.

    **Flujo de Trabajo General:**
    1.  **Identificar Intención:** Primero, entiende si el usuario quiere agendar, consultar, cancelar o reprogramar.
    2.  **Recopilar Información:** Pide al usuario la información que necesites y que no tengas ya en el historial. Para agendar, siempre necesitas un servicio, una fecha y una hora deseada.
    3.  **Manejo de Fechas:** 
        - **Si NO especifica fecha:** Pregunta de manera amigable si tiene alguna fecha en mente. Ejemplo: "¿Tienes alguna fecha en mente para tu cita?😊"
        - **Si SÍ especifica fecha:** Procede a interpretarla usando el contexto temporal.
    4.  **Interpretar Fechas:** Tienes acceso al contexto temporal completo. Convierte expresiones naturales a fechas específicas:
        - "Mañana", "hoy", "el lunes que viene" → usa las fechas calculadas en el contexto
        - "15 de diciembre", "el 25" → calcula la fecha completa (año actual)
        - "Dentro de 3 días", "la próxima semana" → calcula desde la fecha actual
        - Siempre devuelve fechas en formato YYYY-MM-DD para las herramientas
    5.  **Usar Herramientas:** Llama a las herramientas disponibles de forma secuencial. No intentes llamarlas todas a la vez.

    **Flujos Específicos:**
    -   **Agendar Cita (`book_appointment`):**
        -   **Paso 1: Contacto.** ANTES de pedir datos, revisa el historial/contexto para ver si ya conoces al usuario. Si ya tienes su nombre en conversaciones anteriores, úsalo directamente. Si es un usuario completamente nuevo, entonces pide su nombre y apellido. Luego llama a `resolve_contact_on_booking` usando EXACTAMENTE los valores del estado: `organization_id`, `phone_number`, y `country_code` tal como aparecen en "Estado Actual".
        -   **Paso 1.5: Fecha.** Si el usuario no especificó una fecha al solicitar agendar, pregúntale primero por la fecha antes de buscar disponibilidad.
        -   **Paso 2: Disponibilidad.** Llama a `check_availability` usando el `service_id` del estado (NO el nombre del servicio). Siempre muestra al menos 3 opciones al usuario.
        -   **Paso 3: Agendamiento.** Una vez que el usuario elige una hora, llama a `book_appointment` usando EXACTAMENTE el `member_id` que devolvió `check_availability` para esa hora específica. NUNCA uses el `contact_id` como `member_id`. La función `book_appointment` obtendrá automáticamente el `contact_id` del estado global.
        -   **Paso 4: Notificaciones (MUY IMPORTANTE).** Después de un agendamiento exitoso, la herramienta `book_appointment` te devolverá un campo `opt_in_status`.
            -   Si `opt_in_status` es 'opt_in' o 'opt_out', tu trabajo ha terminado. Simplemente confirma la cita.
            -   Si `opt_in_status` es 'not_set', DEBES PREGUNTAR al usuario si desea recibir notificaciones y recordatorios por WhatsApp. Ejemplo: "Tu cita ha sido confirmada. ¿Te gustaría recibir recordatorios por este medio?".
            -   Si el usuario acepta, DEBES llamar a la herramienta `create_whatsapp_opt_in` para guardar su preferencia.

    -   **Reprogramar Cita (`reschedule_appointment`):**
        -   Funciona como una cancelación seguida de un nuevo agendamiento. Primero identifica la cita a reprogramar, luego busca nueva disponibilidad y finalmente llama a `reschedule_appointment`.

    -   **Consultar Citas (`get_user_appointments`):**
        -   Llama a la herramienta para obtener las citas futuras del usuario y muéstraselas de forma clara.

    -   **Cancelar Cita (`cancel_appointment`):**
        -   Si el usuario tiene varias citas, primero muéstraselas con `get_user_appointments` y pregúntale cuál desea cancelar.
        -   Una vez identificada la cita, llama a `cancel_appointment`.

    **Reglas Importantes:**
    -   Sé siempre amable y conversacional.
    -   No inventes información. Si no puedes hacer algo, informa al usuario.
    -   **MEMORIA:** Utiliza la información del historial de la conversación para no volver a preguntar datos que ya tienes. Si en el historial ya tienes el nombre del usuario, úsalo directamente. Si ya agendó antes, no pidas datos personales de nuevo.
    -   **CONTACTOS EXISTENTES:** Cuando `resolve_contact_on_booking` retorne `is_existing_contact: true`, significa que el usuario ya tiene historial. No pidas nombre/apellido nuevamente.
    -   **LENGUAJE NATURAL:** Nunca menciones términos técnicos como "service_id", "contact_id", "UUID", etc. Habla de forma natural sobre "tu servicio", "tu cita", "tu información".
    -   **EMOJIS Y NATURALIDAD:** Usa emojis para hacer la conversación más amigable 😊 NO uses asteriscos (**texto**) ni formato markdown. Escribe de forma natural como en una conversación por WhatsApp.
    -   **FECHAS NATURALES:** Cuando el usuario diga "mañana", "hoy", "el lunes próximo", convierte esas expresiones a fechas específicas usando el contexto temporal que tienes disponible.
    -   **USO DE HERRAMIENTAS:** SIEMPRE usa `service_id` (UUID) para las herramientas `check_availability` y `book_appointment`, NUNCA uses el nombre del servicio. El `service_id` está disponible en el estado.
    """,
)

# --- Herramientas del Agente ---


@appointment_agent.tool
async def resolve_contact_on_booking(
    ctx: RunContext[GlobalState],
    organization_id: str,
    phone_number: str,
    country_code: str,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
) -> ContactResolution:
    """
    Busca un contacto por número de teléfono o lo crea si no existe.
    Esencial para vincular la cita a un contacto.
    
    IMPORTANTE: Si el contacto ya existe, NO necesitas pedir nombre/apellido.
    Solo pide datos si es un usuario completamente nuevo.
    """
    print(f"Resolviendo contacto para la organización {organization_id} y el teléfono {country_code}{phone_number}...")
    print(f"🔍 DEBUG PARÁMETROS:")
    print(f"🔍 organization_id: {organization_id}")
    print(f"🔍 phone_number: {phone_number}")
    print(f"🔍 country_code: {country_code}")

    try:
        import asyncio
        # 1. Buscar si el contacto ya existe
        loop = asyncio.get_event_loop()
        print(f"🔍 Ejecutando consulta: contacts WHERE organization_id='{organization_id}' AND phone='{phone_number}' AND country_code='{country_code}'")
        response = await loop.run_in_executor(
            None,
            lambda: supabase_client.table('contacts').select('id').eq('organization_id', organization_id).eq('phone', phone_number).eq('country_code', country_code).maybe_single().execute()
        )
        print(f"🔍 DEBUG RESPONSE: {type(response)}")
        print(f"🔍 DEBUG RESPONSE.data: {response.data if response else 'Response is None'}")

        if response and response.data:
            contact_id = response.data['id']
            print(f"Contacto encontrado: contact_id='{contact_id}'")
            
            # ✅ ACTUALIZAR EL ESTADO GLOBAL CON EL CONTACT_ID
            # Nota: No podemos modificar ctx.deps directamente, pero podemos devolver
            # el contact_id para que run_appointment_agent lo use para actualizar el estado
            print(f"📋 RESOLVE_CONTACT: contact_id='{contact_id}' listo para actualizar estado")
            
            return ContactResolution(
                success=True,
                contact_id=contact_id,
                message="Contacto existente encontrado. No necesitas pedir más datos.",
                is_existing_contact=True
            )
        else:
            # 2. Si no existe, crear el nuevo contacto
            print("Contacto no encontrado. Creando uno nuevo...")
            
            # Usar placeholders si el nombre o apellido no vienen
            first_name_to_insert = first_name if first_name else "Nuevo"
            last_name_to_insert = last_name if last_name else "Contacto"

            insert_response = await loop.run_in_executor(
                None,
                lambda: supabase_client.table('contacts').insert({
                    'organization_id': organization_id,
                    'phone': phone_number,
                    'country_code': country_code,
                    'first_name': first_name_to_insert,
                    'last_name': last_name_to_insert,
                }).select('id').single().execute()
            )

            if insert_response.data:
                new_contact_id = insert_response.data['id']
                print(f"Nuevo contacto creado con éxito: contact_id='{new_contact_id}'")
                
                # IMPORTANTE: Actualizar el usuario de Zep con los datos reales
                state = ctx.deps
                if state.get("chat_identity_id") and first_name and last_name:
                    from ..zep import update_zep_user_with_real_data
                    user_id = f"chat_{state['chat_identity_id']}"
                    await update_zep_user_with_real_data(user_id, first_name, last_name)
                
                # ✅ CONTACTO NUEVO CREADO - LISTO PARA ACTUALIZAR ESTADO
                print(f"📋 RESOLVE_CONTACT: nuevo contact_id='{new_contact_id}' listo para actualizar estado")
                
                return ContactResolution(
                    success=True,
                    contact_id=new_contact_id,
                    message="Nuevo contacto creado con éxito.",
                    is_existing_contact=False
                )
            else:
                raise Exception("Error al insertar el nuevo contacto en la base de datos.")

    except Exception as e:
        print(f"Error al resolver el contacto: {e}")
        return ContactResolution(
            success=False,
            contact_id=None,
            message=f"Hubo un error al buscar o crear el contacto: {e}",
            is_existing_contact=False
        )


@appointment_agent.tool
async def check_availability(
    ctx: RunContext[GlobalState],
    service_id: str, 
    organization_id: str,
    check_date: str  # Formato 'YYYY-MM-DD'
) -> List[AvailabilitySlot]:
    """
    Verifica la disponibilidad de horarios para un servicio en una fecha específica.
    Devuelve una lista de slots disponibles.
    """
    # (La lógica interna de la función permanece igual)
    print(f"Buscando disponibilidad para el servicio {service_id} en la fecha {check_date}")
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        
        service_response = await loop.run_in_executor(
            None,
            lambda: supabase_client.table('services').select('duration_minutes').eq('id', service_id).maybe_single().execute()
        )
        
        if not service_response.data:
            print(f"❌ No se encontró el servicio con ID: {service_id}")
            return []
            
        duration_minutes = service_response.data['duration_minutes']
        
        day_of_week = datetime.strptime(check_date, '%Y-%m-%d').isoweekday()
        
        org_availability_response = await loop.run_in_executor(
            None,
            lambda: supabase_client.table('organization_availability').select('*').eq('organization_id', organization_id).eq('day_of_week', day_of_week).maybe_single().execute()
        )
        
        if not org_availability_response.data or not org_availability_response.data.get('is_available', False):
                return []

        org_availability = org_availability_response.data
        org_start_time = datetime.strptime(org_availability['start_time'], '%H:%M:%S').time()
        org_end_time = datetime.strptime(org_availability['end_time'], '%H:%M:%S').time()
        
        assigned_members_response = await loop.run_in_executor(
            None,
            lambda: supabase_client.table('service_assignments').select('member_id').eq('service_id', service_id).execute()
        )
        member_ids = [item['member_id'] for item in assigned_members_response.data]
        if not member_ids:
            return []

        appointments_response = await loop.run_in_executor(
            None,
            lambda: supabase_client.table('appointments').select('start_time', 'end_time').eq('organization_id', organization_id).eq('appointment_date', check_date).in_('status', ['programada', 'confirmada']).execute()
        )
        booked_slots = [(datetime.strptime(apt['start_time'], '%H:%M:%S').time(), datetime.strptime(apt['end_time'], '%H:%M:%S').time()) for apt in appointments_response.data]

        available_slots_list = []
        member_availabilities_response = await loop.run_in_executor(
            None,
            lambda: supabase_client.table('member_availability').select('*').in_('member_id', member_ids).eq('day_of_week', day_of_week).execute()
        )

        for member_availability in member_availabilities_response.data:
            member_id = member_availability['member_id']
            member_start = datetime.strptime(member_availability['start_time'], '%H:%M:%S').time()
            member_end = datetime.strptime(member_availability['end_time'], '%H:%M:%S').time()

            actual_start_time = max(org_start_time, member_start)
            actual_end_time = min(org_end_time, member_end)

            current_time = datetime.combine(date.today(), actual_start_time)
            end_boundary = datetime.combine(date.today(), actual_end_time)

            while current_time + timedelta(minutes=duration_minutes) <= end_boundary:
                slot_start = current_time.time()
                slot_end = (current_time + timedelta(minutes=duration_minutes)).time()
                is_booked = any(s_start < slot_end and s_end > slot_start for s_start, s_end in booked_slots)

                if not is_booked:
                    available_slots_list.append(AvailabilitySlot(
                        start_time=slot_start.strftime('%H:%M'),
                        end_time=slot_end.strftime('%H:%M'),
                        member_id=UUID(member_id)  # Convertir string a UUID
                    ))
                current_time += timedelta(minutes=15)

        # Convertir a dict pero manteniendo UUIDs como objetos para la reconstrucción
        unique_slots = []
        seen_slots = set()
        for slot in available_slots_list:
            slot_tuple = (slot.start_time, slot.end_time, str(slot.member_id))
            if slot_tuple not in seen_slots:
                seen_slots.add(slot_tuple)
                unique_slots.append(slot)
                
        sorted_slots = sorted(unique_slots, key=lambda x: datetime.strptime(x.start_time, '%H:%M'))
        return sorted_slots
    except Exception as e:
        print(f"Error en check_availability: {e}")
        return []

@appointment_agent.tool
async def book_appointment(
    ctx: RunContext[GlobalState],
    service_id: str,
    member_id: str,
    appointment_date: str, # Formato YYYY-MM-DD
    start_time: str, # Formato HH:MM
) -> AppointmentConfirmation:
    """
    Crea una cita en la base de datos y luego verifica el estado de opt-in de WhatsApp del contacto.
    Devuelve el ID de la cita y el estado de autorización de WhatsApp.
    
    IMPORTANTE: Obtiene el contact_id del estado global, no como parámetro.
    """
    # ✅ OBTENER CONTACT_ID DEL ESTADO GLOBAL
    state = ctx.deps
    contact_id = state.get("contact_id")
    
    if not contact_id:
        return AppointmentConfirmation(
            success=False,
            message="⚠️ Error: No se encontró el contacto. Por favor, proporciona tu información de contacto primero.",
            opt_in_status="not_set"
        )
    
    print(f"Agendando cita para el contacto_id: {contact_id} en la fecha {appointment_date} a las {start_time}")
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        
        # 1. Crear la cita
        service_response = await loop.run_in_executor(
            None,
            lambda: supabase_client.table('services').select('duration_minutes').eq('id', service_id).maybe_single().execute()
        )
        
        if not service_response.data:
            return {
                "success": False,
                "message": f"No se encontró el servicio con ID: {service_id}"
            }
            
        duration_minutes = service_response.data['duration_minutes']
        
        start_datetime = datetime.fromisoformat(f"{appointment_date}T{start_time}")
        end_datetime = start_datetime + timedelta(minutes=duration_minutes)

        appointment_data = {
            "contact_id": contact_id,
            "service_id": service_id,
            "member_id": str(member_id),  # Convertir UUID a string para la base de datos
            "appointment_date": appointment_date,
            "start_time": start_datetime.strftime('%H:%M:%S'),
            "end_time": end_datetime.strftime('%H:%M:%S'),
            "status": "programada",
            "created_by": str(member_id), # Usar el member_id como created_by (es un perfil válido de la organización)
        }
        response = await loop.run_in_executor(
            None,
            lambda: supabase_client.table('appointments').insert(appointment_data).execute()
        )
        
        if not response.data:
            return AppointmentConfirmation(
                success=False,
                message="No se pudo crear la cita en la base de datos."
            )

        appointment_id = response.data[0]['id']
        print(f"Cita creada con éxito. ID: {appointment_id}")

        # 2. Verificar el estado de opt-in/opt-out de WhatsApp para el contacto
        auth_response = await loop.run_in_executor(
            None,
            lambda: supabase_client.table('contact_authorizations').select('authorization_type').eq('contact_id', contact_id).eq('channel', 'whatsapp').order('created_at', desc=True).limit(1).maybe_single().execute()
        )

        opt_in_status = "not_set"
        if auth_response.data:
            opt_in_status = auth_response.data['authorization_type']
        
        print(f"Estado de opt-in de WhatsApp para el contacto: {opt_in_status}")

        return AppointmentConfirmation(
            success=True,
            appointment_id=UUID(appointment_id) if appointment_id else None,  # Convertir string a UUID
            opt_in_status=opt_in_status,
            message=f"Cita agendada con éxito para el {appointment_date} a las {start_time}."
        )
    except Exception as e:
        print(f"Error al agendar la cita: {e}")
        return AppointmentConfirmation(
            success=False,
            message=f"Hubo un error al agendar la cita: {e}",
        )

@appointment_agent.tool
async def create_whatsapp_opt_in(ctx: RunContext[GlobalState], contact_id: str, organization_id: str) -> Dict[str, Any]:
    """
    Crea un registro de autorización (opt-in) para que un contacto reciba notificaciones de WhatsApp.
    """
    print(f"Creando opt-in de WhatsApp para el contacto_id: {contact_id}")
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        
        # Desactivar cualquier autorización previa para este canal
        await loop.run_in_executor(
            None,
            lambda: supabase_client.table('contact_authorizations').update({'is_active': False}).eq('contact_id', contact_id).eq('channel', 'whatsapp').execute()
        )

        # Insertar el nuevo registro de opt-in
        opt_in_data = {
            'contact_id': contact_id,
            'organization_id': organization_id,
            'authorization_type': 'opt_in',
            'channel': 'whatsapp',
            'is_active': True,
            'created_by': 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11' # TODO: Agente ID
        }
        await loop.run_in_executor(
            None,
            lambda: supabase_client.table('contact_authorizations').insert(opt_in_data).execute()
        )
        
        print("Opt-in de WhatsApp creado con éxito.")
        return {"success": True, "message": "Preferencia de notificaciones guardada."}
    except Exception as e:
        print(f"Error al crear el opt-in de WhatsApp: {e}")
        return {"success": False, "message": f"Hubo un error al guardar la preferencia: {e}"}


@appointment_agent.tool
async def get_user_appointments(
    ctx: RunContext[GlobalState],
    contact_id: str,
    appointment_date: Optional[str] = None,
    time: Optional[str] = None
) -> List[AppointmentInfo]:
    """
    Consulta y devuelve una lista de las citas futuras de un usuario.
    """
    # (La lógica interna de la función permanece igual, pero devuelve List[AppointmentInfo])
    print(f"Buscando citas futuras para el contacto {contact_id}")
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        
        today = date.today().isoformat()
        
        # Construir query como función para ejecutar en executor
        def build_and_execute_query():
            query = supabase_client.table('appointments').select('id, appointment_date, start_time, services(name), profiles(first_name, last_name)').eq('contact_id', contact_id).gte('appointment_date', today).in_('status', ['programada', 'confirmada'])
            if appointment_date:
                query = query.eq('appointment_date', appointment_date)
            if time:
                time_obj = datetime.strptime(time, '%H:%M').strftime('%H:%M:%S')
                query = query.eq('start_time', time_obj)
            return query.order('appointment_date').order('start_time').execute()
        
        response = await loop.run_in_executor(None, build_and_execute_query)
        if not response.data:
            return []

        return [AppointmentInfo(
            appointment_id=UUID(appt['id']),  # Convertir string a UUID
            summary=f"Cita para '{appt['services']['name'] if appt.get('services') else ''}' con {appt['profiles']['first_name'] if appt.get('profiles') else ''} el {appt['appointment_date']} a las {appt['start_time']}"
        ) for appt in response.data]
    except Exception as e:
        print(f"Error al obtener las citas del usuario: {e}")
        return []

@appointment_agent.tool
async def cancel_appointment(ctx: RunContext[GlobalState], appointment_id: str) -> AppointmentConfirmation:
    """
    Cancela una cita actualizando su estado a 'cancelled'.
    """
    # (La lógica interna de la función permanece igual, pero devuelve AppointmentConfirmation)
    print(f"Intentando cancelar la cita {appointment_id}")
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        
        response = await loop.run_in_executor(
            None,
            lambda: supabase_client.table('appointments').update({'status': 'cancelled'}).eq('id', appointment_id).execute()
        )
        if len(response.data) > 0:
            return AppointmentConfirmation(success=True, message="Tu cita ha sido cancelada con éxito.")
        else:
            raise Exception("No se pudo actualizar la cita.")
    except Exception as e:
        return AppointmentConfirmation(success=False, message="Lo siento, no pude cancelar tu cita.")

# (Las herramientas `confirm_appointment` y `reschedule_appointment` seguirían un patrón similar)
# ...

# --- Función de Entrada (Entrypoint) para el Grafo ---
from langgraph.types import Command
from langchain_core.messages import AIMessage

async def run_appointment_agent(state: GlobalState) -> Command:
    """
    Punto de entrada para ejecutar el agente de citas.
    Ahora incluye contexto de memoria de Zep y usa Command pattern para evitar loops.
    """
    print("--- Ejecutando Appointment Agent ---")
    
    # DEBUG: Mostrar el estado actual
    print(f"🔍 DEBUG Estado actual:")
    print(f"🔍 service_id: {state.get('service_id')}")
    print(f"🔍 service_name: {state.get('service_name')}")
    print(f"🔍 organization_id: {state.get('organization_id')}")
    
    # VALIDACIÓN: Solo delegar al KnowledgeAgent si NO hay service_id
    if not state.get('service_id'):
        print(f"🔍 No hay service_id en estado, delegando al KnowledgeAgent para resolver servicio")
        # Crear un mensaje que indique al knowledge_agent que resuelva el servicio
        from langchain_core.messages import HumanMessage
        current_messages = state.get("messages", [])
        resolve_message = HumanMessage(content="Necesito encontrar el servicio específico que quiere agendar basándose en el contexto de la conversación.")
        
        # Actualizar estado con el mensaje de resolución y delegar
        return Command(
            update={"messages": current_messages + [resolve_message]},
            goto="KnowledgeAgent"
        )

    # Obtener contexto de memoria de Zep si hay chat_identity_id
    zep_context = ""
    if state.get("chat_identity_id"):
        thread_id = state['chat_identity_id']
        try:
            zep_context = await get_zep_memory_context(thread_id, min_rating=0.0)
            print(f"🧠 DEBUG Contexto Zep: {zep_context[:200]}...")
        except Exception as e:
            print(f"❌ Error obteniendo contexto de Zep thread {thread_id}: {e}")
            zep_context = ""
    
    # Obtener fecha y hora actual en zona horaria de Colombia
    
    colombia_tz = pytz.timezone('America/Bogota')
    now = datetime.now(colombia_tz)
    
    # Formatear información temporal completa
    current_date = now.strftime('%Y-%m-%d')
    current_time = now.strftime('%H:%M')
    current_weekday = now.strftime('%A')  # Día de la semana en inglés
    current_day_spanish = {
        'Monday': 'Lunes', 'Tuesday': 'Martes', 'Wednesday': 'Miércoles', 
        'Thursday': 'Jueves', 'Friday': 'Viernes', 'Saturday': 'Sábado', 'Sunday': 'Domingo'
    }.get(current_weekday, current_weekday)
    
    # Calcular fechas útiles para referencia
    tomorrow = (now + timedelta(days=1)).strftime('%Y-%m-%d')
    next_week_start = (now + timedelta(days=(7 - now.weekday()))).strftime('%Y-%m-%d')
    
    # Días de la próxima semana
    next_week_days = {}
    for i, day_name in enumerate(['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo']):
        next_week_date = (now + timedelta(days=(7 - now.weekday() + i))).strftime('%Y-%m-%d')
        next_week_days[day_name] = next_week_date
    
    # Preparamos el contexto para el agente, incluyendo el historial y datos clave del estado.
    # Esto es crucial para que el LLM tenga toda la información necesaria.
    input_prompt = f"""
    CONTEXTO TEMPORAL ACTUAL (Zona horaria: America/Bogota):
    - Fecha de hoy: {current_date} ({current_day_spanish})
    - Hora actual: {current_time}
    - Mañana: {tomorrow}
    - Próxima semana inicia: {next_week_start}
    
    DÍAS DE LA PRÓXIMA SEMANA (para referencias como "el lunes que viene"):
    - Lunes próximo: {next_week_days['Lunes']}
    - Martes próximo: {next_week_days['Martes']}
    - Miércoles próximo: {next_week_days['Miércoles']}
    - Jueves próximo: {next_week_days['Jueves']}
    - Viernes próximo: {next_week_days['Viernes']}
    - Sábado próximo: {next_week_days['Sábado']}
    - Domingo próximo: {next_week_days['Domingo']}
    
    INSTRUCCIONES DE INTERPRETACIÓN:
    - "Mañana" = {tomorrow}
    - "El lunes" o "el lunes que viene" = {next_week_days['Lunes']}
    - "La próxima semana" = cualquier día desde {next_week_start}
    - Para fechas específicas como "15 de diciembre", calcula el año actual (usa {now.year} si no especifican)
    - Para "dentro de X días/semanas", calcula desde la fecha actual
    
    Historial de la Conversación:
    {state['messages']}

    Estado Actual (INFORMACIÓN INTERNA - NO MOSTRAR AL USUARIO):
    - Servicio seleccionado ID: {state.get('service_id')}
    - Servicio seleccionado nombre: {state.get('service_name')}
    - Contacto del usuario: {state.get('contact_id')}
    - Organización: {state.get('organization_id')}
    - Teléfono: {state.get('phone_number')} (número nacional)
    - Código país: {state.get('country_code')} (código de país)
    - Cita en proceso: {state.get('focused_appointment')}
    - Horarios disponibles: {state.get('available_slots')}
    
    {"Contexto de Memoria Zep: " + zep_context if zep_context else ""}
    
    Último Mensaje del Usuario: "{state['messages'][-1].content}"

    Por favor, actúa según tu flujo de trabajo y el último mensaje del usuario.
    """

    result = await appointment_agent.run(input_prompt, deps=state)
    
    tool_output = result.output

    # Obtener mensajes actuales para conservar el historial
    current_messages = state.get("messages", [])

    # Procesamos la salida de la herramienta para actualizar el estado del grafo
    
    # ✅ MANEJAR RESOLUCIÓN DE CONTACTO Y ACTUALIZAR ESTADO
    if isinstance(tool_output, ContactResolution):
        print(f"📋 APPOINTMENT_AGENT: Procesando ContactResolution")
        print(f"📋 tool_output.success: {tool_output.success}")
        print(f"📋 tool_output.contact_id: {tool_output.contact_id}")
        
        if tool_output.success and tool_output.contact_id:
            # ✅ ACTUALIZAR EL ESTADO GLOBAL CON EL CONTACT_ID
            print(f"📋 ACTUALIZANDO ESTADO: contact_id = {tool_output.contact_id}")
            ai_message = AIMessage(content=tool_output.message, name="AppointmentAgent")
            return Command(
                update={
                    "messages": current_messages + [ai_message],
                    "contact_id": tool_output.contact_id  # ✅ AQUÍ SE ACTUALIZA EL ESTADO
                },
                goto="Supervisor"  # Continuar el flujo
            )
        else:
            # Error en la resolución del contacto
            ai_message = AIMessage(content=f"⚠️ {tool_output.message}", name="AppointmentAgent")
            return Command(
                update={"messages": current_messages + [ai_message]},
                goto="__end__"
            )
    
    if isinstance(tool_output, list) and all(isinstance(i, AvailabilitySlot) for i in tool_output):
        slots = [s.dict() for s in tool_output]
        if not slots:
            ai_message = AIMessage(content="Lo siento, no encontré horarios disponibles. ¿Quieres intentar con otra fecha?", name="AppointmentAgent")
            return Command(
                update={"messages": current_messages + [ai_message]},
                goto="__end__"
            )
        
        formatted_slots = ", ".join(sorted(list(set([s['start_time'] for s in slots]))))
        date_used = "la fecha solicitada" # Simplificación, el agente debería extraer esto
        ai_message = AIMessage(content=f"Para {date_used} tengo estos horarios: {formatted_slots}. ¿Cuál prefieres?", name="AppointmentAgent")
        return Command(
            update={
                "messages": current_messages + [ai_message],
                "available_slots": slots
            },
            goto="Supervisor"  # Regresar al supervisor para siguiente interacción
        )

    if isinstance(tool_output, AppointmentConfirmation):
        ai_message = AIMessage(content=tool_output.message, name="AppointmentAgent")
        return Command(
            update={
                "messages": current_messages + [ai_message],
                "focused_appointment": None,
                "available_slots": None
            },
            goto="__end__"  # Cita confirmada, terminar
        )

    if isinstance(tool_output, list) and all(isinstance(i, AppointmentInfo) for i in tool_output):
        appointments = [a.dict() for a in tool_output]
        if not appointments:
            ai_message = AIMessage(content="No encontré citas futuras para ti.", name="AppointmentAgent")
            return Command(
                update={"messages": current_messages + [ai_message]},
                goto="__end__"
            )
        elif len(appointments) == 1:
            ai_message = AIMessage(content=f"Encontré esta cita: {appointments[0]['summary']}. ¿Qué deseas hacer con ella?", name="AppointmentAgent")
            return Command(
                update={
                    "messages": current_messages + [ai_message],
                    "focused_appointment": appointments[0]
                },
                goto="Supervisor"  # Regresar al supervisor para siguiente acción
            )
        else:
            summaries = "\n- ".join([a['summary'] for a in appointments])
            ai_message = AIMessage(content=f"Encontré estas citas:\n- {summaries}\n\n¿Sobre cuál quieres realizar una acción?", name="AppointmentAgent")
            return Command(
                update={"messages": current_messages + [ai_message]},
                goto="Supervisor"  # Regresar al supervisor para siguiente acción
            )

    # Fallback por si el agente responde con texto plano
    if isinstance(tool_output, str):
        ai_message = AIMessage(content=tool_output, name="AppointmentAgent")
        return Command(
            update={"messages": current_messages + [ai_message]},
            goto="__end__"
        )

    # Caso por defecto
    ai_message = AIMessage(content="No estoy seguro de cómo proceder.", name="AppointmentAgent")
    return Command(
        update={"messages": current_messages + [ai_message]},
        goto="__end__"
    ) 