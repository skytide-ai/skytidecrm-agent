from typing import Dict, Any, List, Optional
import datetime
from pydantic import BaseModel, Field
import pydantic_ai
from openai import OpenAI
from pydantic_ai import Agent, RunContext

# Importamos el estado global y el cliente de Supabase
from ..state import GlobalState
from ..db import supabase_client

# --- Cliente Pydantic AI ---
client = OpenAI()

# --- Modelos de Datos para Herramientas ---
class AvailabilitySlot(BaseModel):
    start_time: str
    end_time: str
    member_id: str

class AppointmentConfirmation(BaseModel):
    success: bool
    appointment_id: Optional[str] = None
    message: str
    opt_in_status: Optional[str] = "not_set" # Puede ser "opt_in", "opt_out", o "not_set"

class AppointmentInfo(BaseModel):
    appointment_id: str
    summary: str

# --- Definición del Agente de Citas ---
appointment_agent = Agent(
    'openai:gpt-4-turbo',
    system_prompt="""
    Eres un asistente experto en la gestión de citas. Tu trabajo es guiar al usuario a través del proceso de agendamiento, consulta, cancelación y reprogramación.

    **Flujo de Trabajo General:**
    1.  **Identificar Intención:** Primero, entiende si el usuario quiere agendar, consultar, cancelar o reprogramar.
    2.  **Recopilar Información:** Pide al usuario la información que necesites y que no tengas ya en el historial. Para agendar, siempre necesitas un servicio, una fecha y una hora deseada.
    3.  **Usar Herramientas:** Llama a las herramientas disponibles de forma secuencial. No intentes llamarlas todas a la vez.

    **Flujos Específicos:**
    -   **Agendar Cita (`book_appointment`):**
        -   **Paso 1: Contacto.** Llama a `resolve_contact_on_booking` para obtener el `contact_id`. Si el usuario es nuevo, pide su nombre y apellido.
        -   **Paso 2: Disponibilidad.** Llama a `check_availability` para encontrar horarios. Siempre muestra al menos 3 opciones al usuario.
        -   **Paso 3: Agendamiento.** Una vez que el usuario elige una hora, llama a `book_appointment`.
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
    -   Utiliza la información del historial de la conversación para no volver a preguntar datos que ya tienes.
    """,
)

# --- Herramientas del Agente ---

@appointment_agent.tool
async def resolve_contact_on_booking(
    organization_id: str,
    phone_number: str,
    country_code: str,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Busca un contacto por número de teléfono o lo crea si no existe.
    Esencial para vincular la cita a un contacto.
    """
    print(f"Resolviendo contacto para la organización {organization_id} y el teléfono {country_code}{phone_number}...")

    try:
        # 1. Buscar si el contacto ya existe
        response = await supabase_client.table('contacts').select('id').eq('organization_id', organization_id).eq('phone', phone_number).eq('country_code', country_code).maybe_single().execute()

        if response.data:
            contact_id = response.data['id']
            print(f"Contacto encontrado: contact_id='{contact_id}'")
            return {
                "success": True,
                "contact_id": contact_id,
                "message": "Contacto existente encontrado.",
            }
        else:
            # 2. Si no existe, crear el nuevo contacto
            print("Contacto no encontrado. Creando uno nuevo...")
            
            # Usar placeholders si el nombre o apellido no vienen
            first_name_to_insert = first_name if first_name else "Nuevo"
            last_name_to_insert = last_name if last_name else "Contacto"

            insert_response = await supabase_client.table('contacts').insert({
                'organization_id': organization_id,
                'phone': phone_number,
                'country_code': country_code,
                'first_name': first_name_to_insert,
                'last_name': last_name_to_insert,
            }).select('id').single().execute()

            if insert_response.data:
                new_contact_id = insert_response.data['id']
                print(f"Nuevo contacto creado con éxito: contact_id='{new_contact_id}'")
                return {
                    "success": True,
                    "contact_id": new_contact_id,
                    "message": "Nuevo contacto creado con éxito.",
                }
            else:
                raise Exception("Error al insertar el nuevo contacto en la base de datos.")

    except Exception as e:
        print(f"Error al resolver el contacto: {e}")
        return {
            "success": False,
            "contact_id": None,
            "message": f"Hubo un error al buscar o crear el contacto: {e}",
        }


@appointment_agent.tool
async def check_availability(
    service_id: str, 
    organization_id: str,
    date: str  # Formato 'YYYY-MM-DD'
) -> List[AvailabilitySlot]:
    """
    Verifica la disponibilidad de horarios para un servicio en una fecha específica.
    Devuelve una lista de slots disponibles.
    """
    # (La lógica interna de la función permanece igual)
    print(f"Buscando disponibilidad para el servicio {service_id} en la fecha {date}")
    try:
        service_response = await supabase_client.table('services').select('duration_minutes').eq('id', service_id).single().execute()
        duration_minutes = service_response.data['duration_minutes']
        
        day_of_week = datetime.datetime.strptime(date, '%Y-%m-%d').isoweekday()
        
        org_availability_response = await supabase_client.table('organization_availability').select('*').eq('organization_id', organization_id).eq('day_of_week', day_of_week).maybe_single().execute()
        
        if not org_availability_response.data or not org_availability_response.data.get('is_available', False):
                return []

        org_availability = org_availability_response.data
        org_start_time = datetime.datetime.strptime(org_availability['start_time'], '%H:%M:%S').time()
        org_end_time = datetime.datetime.strptime(org_availability['end_time'], '%H:%M:%S').time()
        
        assigned_members_response = await supabase_client.table('service_assignments').select('member_id').eq('service_id', service_id).execute()
        member_ids = [item['member_id'] for item in assigned_members_response.data]
        if not member_ids:
            return []

        appointments_response = await supabase_client.table('appointments').select('start_time', 'end_time').eq('organization_id', organization_id).eq('appointment_date', date).in_('status', ['programada', 'confirmada']).execute()
        booked_slots = [(datetime.datetime.strptime(apt['start_time'], '%H:%M:%S').time(), datetime.datetime.strptime(apt['end_time'], '%H:%M:%S').time()) for apt in appointments_response.data]

        available_slots_list = []
        member_availabilities_response = await supabase_client.table('member_availability').select('*').in_('member_id', member_ids).eq('day_of_week', day_of_week).execute()

        for member_availability in member_availabilities_response.data:
            member_id = member_availability['member_id']
            member_start = datetime.datetime.strptime(member_availability['start_time'], '%H:%M:%S').time()
            member_end = datetime.datetime.strptime(member_availability['end_time'], '%H:%M:%S').time()

            actual_start_time = max(org_start_time, member_start)
            actual_end_time = min(org_end_time, member_end)

            current_time = datetime.datetime.combine(datetime.date.today(), actual_start_time)
            end_boundary = datetime.datetime.combine(datetime.date.today(), actual_end_time)

            while current_time + datetime.timedelta(minutes=duration_minutes) <= end_boundary:
                slot_start = current_time.time()
                slot_end = (current_time + datetime.timedelta(minutes=duration_minutes)).time()
                is_booked = any(s_start < slot_end and s_end > slot_start for s_start, s_end in booked_slots)

                if not is_booked:
                    available_slots_list.append(AvailabilitySlot(
                        start_time=slot_start.strftime('%H:%M'),
                        end_time=slot_end.strftime('%H:%M'),
                        member_id=member_id
                    ))
                current_time += datetime.timedelta(minutes=15)

        unique_slots = [dict(t) for t in {tuple(d.items()) for d in [s.dict() for s in available_slots_list]}]
        sorted_slots = sorted(unique_slots, key=lambda x: datetime.datetime.strptime(x['start_time'], '%H:%M'))

        return [AvailabilitySlot(**s) for s in sorted_slots]
    except Exception as e:
        print(f"Error en check_availability: {e}")
        return []

@appointment_agent.tool
async def book_appointment(
    contact_id: str,
    service_id: str,
    member_id: str,
    appointment_date: str, # Formato YYYY-MM-DD
    start_time: str, # Formato HH:MM
) -> AppointmentConfirmation:
    """
    Crea una cita en la base de datos y luego verifica el estado de opt-in de WhatsApp del contacto.
    Devuelve el ID de la cita y el estado de autorización de WhatsApp.
    """
    print(f"Agendando cita para el contacto_id: {contact_id} en la fecha {appointment_date} a las {start_time}")
    try:
        # 1. Crear la cita
        service_response = await supabase_client.table('services').select('duration_minutes').eq('id', service_id).single().execute()
        duration_minutes = service_response.data['duration_minutes']
        
        start_datetime = datetime.datetime.fromisoformat(f"{appointment_date}T{start_time}")
        end_datetime = start_datetime + datetime.timedelta(minutes=duration_minutes)

        appointment_data = {
            "contact_id": contact_id,
            "service_id": service_id,
            "member_id": member_id,
            "appointment_date": appointment_date,
            "start_time": start_datetime.strftime('%H:%M:%S'),
            "end_time": end_datetime.strftime('%H:%M:%S'),
            "status": "programada",
            "created_by": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", # TODO: Reemplazar con el ID del usuario/agente real
        }
        response = await supabase_client.table('appointments').insert(appointment_data).execute()
        
        if not response.data:
            return AppointmentConfirmation(
                success=False,
                message="No se pudo crear la cita en la base de datos."
            )

        appointment_id = response.data[0]['id']
            print(f"Cita creada con éxito. ID: {appointment_id}")

        # 2. Verificar el estado de opt-in/opt-out de WhatsApp para el contacto
        auth_response = await supabase_client.table('contact_authorizations').select('authorization_type').eq('contact_id', contact_id).eq('channel', 'whatsapp').order('created_at', desc=True).limit(1).maybe_single().execute()

        opt_in_status = "not_set"
        if auth_response.data:
            opt_in_status = auth_response.data['authorization_type']
        
        print(f"Estado de opt-in de WhatsApp para el contacto: {opt_in_status}")

        return AppointmentConfirmation(
            success=True,
            appointment_id=appointment_id,
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
async def create_whatsapp_opt_in(contact_id: str, organization_id: str) -> Dict[str, Any]:
    """
    Crea un registro de autorización (opt-in) para que un contacto reciba notificaciones de WhatsApp.
    """
    print(f"Creando opt-in de WhatsApp para el contacto_id: {contact_id}")
    try:
        # Desactivar cualquier autorización previa para este canal
        await supabase_client.table('contact_authorizations').update({'is_active': False}).eq('contact_id', contact_id).eq('channel', 'whatsapp').execute()

        # Insertar el nuevo registro de opt-in
        opt_in_data = {
            'contact_id': contact_id,
            'organization_id': organization_id,
            'authorization_type': 'opt_in',
            'channel': 'whatsapp',
            'is_active': True,
            'created_by': 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11' # TODO: Agente ID
        }
        await supabase_client.table('contact_authorizations').insert(opt_in_data).execute()
        
        print("Opt-in de WhatsApp creado con éxito.")
        return {"success": True, "message": "Preferencia de notificaciones guardada."}
    except Exception as e:
        print(f"Error al crear el opt-in de WhatsApp: {e}")
        return {"success": False, "message": f"Hubo un error al guardar la preferencia: {e}"}


@appointment_agent.tool
async def get_user_appointments(
    contact_id: str,
    date: Optional[str] = None,
    time: Optional[str] = None
) -> List[AppointmentInfo]:
    """
    Consulta y devuelve una lista de las citas futuras de un usuario.
    """
    # (La lógica interna de la función permanece igual, pero devuelve List[AppointmentInfo])
    print(f"Buscando citas futuras para el contacto {contact_id}")
    try:
        today = datetime.date.today().isoformat()
        query = supabase_client.table('appointments').select('id, appointment_date, start_time, services(name), profiles(first_name, last_name)').eq('contact_id', contact_id).gte('appointment_date', today).in_('status', ['programada', 'confirmada'])
        if date:
            query = query.eq('appointment_date', date)
        if time:
                time_obj = datetime.datetime.strptime(time, '%H:%M').strftime('%H:%M:%S')
                query = query.eq('start_time', time_obj)

        response = await query.order('appointment_date').order('start_time').execute()
        if not response.data:
            return []
            
        return [AppointmentInfo(
            appointment_id=appt['id'],
            summary=f"Cita para '{appt['services']['name'] if appt.get('services') else ''}' con {appt['profiles']['first_name'] if appt.get('profiles') else ''} el {appt['appointment_date']} a las {appt['start_time']}"
        ) for appt in response.data]
    except Exception as e:
        print(f"Error al obtener las citas del usuario: {e}")
        return []

@appointment_agent.tool
async def cancel_appointment(appointment_id: str) -> AppointmentConfirmation:
    """
    Cancela una cita actualizando su estado a 'cancelled'.
    """
    # (La lógica interna de la función permanece igual, pero devuelve AppointmentConfirmation)
    print(f"Intentando cancelar la cita {appointment_id}")
    try:
        response = await supabase_client.table('appointments').update({'status': 'cancelled'}).eq('id', appointment_id).execute()
        if len(response.data) > 0:
            return AppointmentConfirmation(success=True, message="Tu cita ha sido cancelada con éxito.")
        else:
            raise Exception("No se pudo actualizar la cita.")
    except Exception as e:
        return AppointmentConfirmation(success=False, message="Lo siento, no pude cancelar tu cita.")

# (Las herramientas `confirm_appointment` y `reschedule_appointment` seguirían un patrón similar)
# ...

# --- Función de Entrada (Entrypoint) para el Grafo ---
async def run_appointment_agent(state: GlobalState) -> Dict[str, Any]:
    """
    Punto de entrada para ejecutar el agente de citas.
    """
    print("--- Ejecutando Appointment Agent ---")
    
    # Preparamos el contexto para el agente, incluyendo el historial y datos clave del estado.
    # Esto es crucial para que el LLM tenga toda la información necesaria.
    input_prompt = f"""
    Historial de la Conversación:
    {state['messages']}

    Estado Actual:
    - service_id: {state.get('service_id')}
    - contact_id: {state.get('contact_id')}
    - organization_id: {state.get('organization_id')}
    - phone_number: {state.get('phone_number')} (número nacional)
    - country_code: {state.get('country_code')} (código de país)
    - Cita en Foco (si aplica): {state.get('focused_appointment')}
    - Horarios Ofrecidos (si aplica): {state.get('available_slots')}
    
    Último Mensaje del Usuario: "{state['messages'][-1].content}"

    Por favor, actúa según tu flujo de trabajo y el último mensaje del usuario.
    """

    result = await appointment_agent.run(input_prompt, client=client)
    
    tool_output = result.output

    # Procesamos la salida de la herramienta para actualizar el estado del grafo
    if isinstance(tool_output, list) and all(isinstance(i, AvailabilitySlot) for i in tool_output):
        slots = [s.dict() for s in tool_output]
        if not slots:
            return {"messages": [("ai", "Lo siento, no encontré horarios disponibles. ¿Quieres intentar con otra fecha?")]}
        
        formatted_slots = ", ".join(sorted(list(set([s['start_time'] for s in slots]))))
        date_used = "la fecha solicitada" # Simplificación, el agente debería extraer esto
        return {
            "messages": [("ai", f"Para {date_used} tengo estos horarios: {formatted_slots}. ¿Cuál prefieres?")],
            "available_slots": slots,
        }

    if isinstance(tool_output, AppointmentConfirmation):
        return {
            "messages": [("ai", tool_output.message)],
            "focused_appointment": None,
            "available_slots": None,
        }

    if isinstance(tool_output, list) and all(isinstance(i, AppointmentInfo) for i in tool_output):
        appointments = [a.dict() for a in tool_output]
        if not appointments:
            return {"messages": [("ai", "No encontré citas futuras para ti.")]}
        elif len(appointments) == 1:
            return {
                "messages": [("ai", f"Encontré esta cita: {appointments[0]['summary']}. ¿Qué deseas hacer con ella?")],
                "focused_appointment": appointments[0]
            }
        else:
            summaries = "\n- ".join([a['summary'] for a in appointments])
            return {"messages": [("ai", f"Encontré estas citas:\n- {summaries}\n\n¿Sobre cuál quieres realizar una acción?")]}

    # Fallback por si el agente responde con texto plano
    if isinstance(tool_output, str):
        return {"messages": [("ai", tool_output)]}

    return {"messages": [("ai", "No estoy seguro de cómo proceder.")]} 