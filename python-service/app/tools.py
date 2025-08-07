from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta, date
from pydantic import BaseModel, Field, field_validator
from uuid import UUID
import asyncio
import json
import re
import pytz
from openai import AsyncOpenAI

from .state import GlobalState
from .db import supabase_client, run_db
from langchain_core.tools import tool

# --- Cliente OpenAI Asíncrono ---
aclient = AsyncOpenAI()

# --- Funciones Auxiliares ---
def is_valid_uuid(uuid_to_test, version=4):
    """Verifica si un string es un UUID válido."""
    try:
        uuid_obj = UUID(uuid_to_test, version=version)
    except ValueError:
        return False
    return str(uuid_obj) == uuid_to_test

def parse_markdown_to_json(markdown_text: str) -> Dict[str, Any]:
    """Parsea un texto en markdown con secciones a un diccionario JSON."""
    data = {}
    sections = re.split(r'\n##\s+', markdown_text)
    main_title_match = re.match(r'#\s+(.*)', sections[0])
    if main_title_match:
        data['title'] = main_title_match.group(1).strip()
        content_after_title = sections[0][main_title_match.end():].strip()
        if content_after_title:
            data['summary'] = content_after_title
    
    for section in sections[1:]:
        lines = section.split('\n')
        title = lines[0].strip()
        content = "\n".join(lines[1:]).strip()
        
        if 'Información Rápida' in title:
            info_rapida = {}
            items = re.findall(r'-\s+\*\*(.*?):\*\*\s+(.*)', content)
            for key, value in items:
                info_rapida[key.strip().lower().replace(' ', '_')] = value.strip()
            data['informacion_rapida'] = info_rapida
        else:
            key = title.lower().replace(' ', '_')
            data[key] = content
            
    return data

# --- Modelos de Datos para Herramientas ---

class AvailabilitySlot(BaseModel):
    start_time: str = Field(description="Hora de inicio en formato HH:MM")
    end_time: str = Field(description="Hora de fin en formato HH:MM")
    member_id: UUID = Field(description="ID único del miembro (UUID válido)")
    
    @field_validator('start_time', 'end_time', mode='before')
    @classmethod
    def validate_time_format(cls, v):
        try:
            datetime.strptime(v, '%H:%M')
            return v
        except ValueError:
            raise ValueError("Formato de hora debe ser HH:MM")

class AppointmentConfirmation(BaseModel):
    success: bool
    appointment_id: Optional[UUID] = None
    message: str
    opt_in_status: Optional[str] = "not_set"

class AppointmentInfo(BaseModel):
    appointment_id: UUID
    summary: str

class ContactResolution(BaseModel):
    success: bool
    contact_id: Optional[str] = None
    message: str
    is_existing_contact: bool = False

class SlotSelection(BaseModel):
    success: bool
    message: str
    selected_date: str
    selected_time: str
    member_id: str = ""

class ContextReset(BaseModel):
    success: bool
    message: str
    fields_cleared: List[str]

# --- Funciones de Herramientas ---

async def generate_embedding(text: str) -> List[float]:
    try:
        response = await aclient.embeddings.create(model="text-embedding-3-small", input=text)
        return response.data[0].embedding
    except Exception as e:
        print(f"❌ Error generando embedding: {e}")
        return []

async def search_knowledge_semantic(query: str, organization_id: str, service_id: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
    try:
        query_embedding = await generate_embedding(query)
        if not query_embedding:
            return []
        
        rpc_params = {
            'query_embedding': query_embedding,
            'match_threshold': 0.1,
            'match_count': limit,
            'org_id': organization_id,
            'p_service_id': service_id
        }
        
        print(f"🔍 Parámetros RPC: {rpc_params}")
        result = await run_db(lambda: supabase_client.rpc('match_documents_by_org', rpc_params).execute())
        
        print(f"📊 Resultados brutos encontrados: {len(result.data) if result.data else 0}")
        if result.data:
            print(f"📋 Primer resultado bruto: {result.data[0]}")
        return result.data if result.data else []
    except Exception as e:
        print(f"❌ Error en búsqueda semántica RPC: {e}")
        return []

@tool
async def knowledge_search(organization_id: str, query: str, service_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Busca información de servicios en la base de conocimiento."""
    print(f"--- 🛠️ Herramienta: knowledge_search ---")
    print(f"🔍 Parámetros recibidos: query='{query}', organization_id='{organization_id}', service_id='{service_id}'")

    if not is_valid_uuid(organization_id):
        error_msg = f"Error de validación: organization_id '{organization_id}' no es un UUID válido."
        print(f"❌ {error_msg}")
        return [{"success": False, "message": error_msg}]
    
    matching_results = await search_knowledge_semantic(query, organization_id, service_id=service_id)
    
    if not matching_results:
        print("🤷 No se encontraron resultados en la búsqueda semántica.")
        return [{"success": False, "message": "No encontré información sobre eso. ¿Puedes preguntarme de otra manera?"}]

    simplified_results = []
    for result in matching_results[:3]:
        metadata = result.get("metadata", {})
        service_id_res = metadata.get("service_id")
        if service_id_res:
            structured_content = parse_markdown_to_json(result.get("content", ""))
            simplified = {
                "success": True,
                "service_id": service_id_res,
                "service_name": structured_content.get("title", "Servicio sin título"),
                "details": structured_content
            }
            simplified_results.append(simplified)
        else:
            # Pasar documentos generales (files) para responder dudas generales
            content_text = result.get("content", "")
            title = metadata.get("title") or metadata.get("file_name") or "Documento"
            simplified_results.append({
                "success": True,
                "service_id": None,
                "service_name": None,
                "details": {"title": title, "summary": content_text[:800]},
                "source_type": metadata.get("source_type"),
            })
    
    if simplified_results:
        print(f"✅ Devolviendo {len(simplified_results)} resultados simplificados al agente.")
        print(json.dumps(simplified_results, indent=2))
        return simplified_results
    else:
        print("❌ No se encontraron servicios válidos después de procesar los resultados brutos.")
        return [{"success": False, "message": "No encontré servicios específicos para esa consulta."}]

@tool
def update_service_in_state(service_id: str, service_name: str) -> Dict[str, Any]:
    """Confirma el servicio seleccionado. Devuelve una carga útil estructurada para actualizar el estado."""
    print(f"--- 🛠️ Herramienta: update_service_in_state ---")
    print(f"✅ Guardando service_id: {service_id}, service_name: {service_name}")
    return {
        "success": True,
        "action": "update_service",
        "service_id": service_id,
        "service_name": service_name,
    }

@tool
async def reset_appointment_context(reason: str = "Cambio de contexto detectado") -> ContextReset:
    """Resetea el contexto de agendamiento."""
    fields_to_clear = ["available_slots", "selected_date", "selected_time", "selected_member_id"]
    print(f"🔄 RESET CONTEXTO: {reason}")
    return ContextReset(success=True, message=f"Entendido! Empezamos de nuevo.", fields_cleared=fields_to_clear)

@tool
async def select_appointment_slot(available_slots: List[Dict[str, Any]], appointment_date: str, start_time: str) -> SlotSelection:
    """Selecciona un horario específico de los slots disponibles."""
    selected_slot = next((slot for slot in available_slots if slot.get("start_time") == start_time), None)
    if not selected_slot:
        try:
            print(f"[select_appointment_slot] start_time buscado={start_time} | primeros_slots={available_slots[:3]}")
        except Exception:
            pass
        return SlotSelection(success=False, message=f"No encontré el horario {start_time}.", selected_date="", selected_time="", member_id="")
    
    member_id = str(selected_slot.get("member_id"))
    print(f"📅 SLOT SELECCIONADO: fecha={appointment_date}, hora={start_time}, member_id={member_id}")
    return SlotSelection(success=True, message=f"Perfecto! Has seleccionado para el {appointment_date} a las {start_time}.", selected_date=appointment_date, selected_time=start_time, member_id=member_id)

@tool
def resolve_relative_date(date_text: str, timezone: str = "America/Bogota") -> Dict[str, Any]:
    """Resuelve expresiones de fecha relativas en español (p. ej., 'hoy', 'mañana', 'la otra semana') a 'YYYY-MM-DD' usando la zona horaria indicada."""
    try:
        import unicodedata
        def _normalize(s: str) -> str:
            s = s.lower().strip()
            return ''.join(c for c in unicodedata.normalize('NFKD', s) if not unicodedata.combining(c))

        tz = pytz.timezone(timezone)
        today = datetime.now(tz).date()
        raw = date_text
        text = _normalize(date_text)

        # Casos directos
        if text in ("hoy",):
            resolved = today
        elif text in ("manana", "mañana"):
            resolved = today + timedelta(days=1)
        elif text in ("pasado manana", "pasado mañana"):
            resolved = today + timedelta(days=2)
        elif text in ("la otra semana", "la proxima semana", "la próxima semana", "proxima semana", "pr\u00f3xima semana"):
            resolved = today + timedelta(days=7)
        else:
            # Formatos comunes: YYYY-MM-DD, DD/MM, DD-MM
            import re
            iso_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
            if iso_match:
                return {"success": True, "action": "set_selected_date", "selected_date": iso_match.group(1), "source_text": raw, "timezone": timezone}
            dm_match = re.search(r"\b(\d{1,2})[/-](\d{1,2})\b", text)
            if dm_match:
                d = int(dm_match.group(1)); m = int(dm_match.group(2))
                y = today.year
                try:
                    candidate = date(y, m, d)
                except ValueError:
                    return {"success": False, "message": "Fecha inválida."}
                # Si ya pasó, asumir próximo año
                if candidate < today:
                    try:
                        candidate = date(y + 1, m, d)
                    except ValueError:
                        return {"success": False, "message": "Fecha inválida."}
                return {"success": True, "action": "set_selected_date", "selected_date": candidate.isoformat(), "source_text": raw, "timezone": timezone}
            return {"success": False, "message": "No pude interpretar la fecha."}

        return {"success": True, "action": "set_selected_date", "selected_date": resolved.isoformat(), "source_text": raw, "timezone": timezone}
    except Exception as e:
        return {"success": False, "message": f"Error resolviendo fecha: {e}"}

@tool
async def resolve_contact_on_booking(organization_id: str, phone_number: str, country_code: str, first_name: Optional[str] = None, last_name: Optional[str] = None) -> ContactResolution:
    """Busca un contacto por teléfono o lo crea si no existe."""
    try:
        response = await run_db(lambda: supabase_client.table('contacts').select('id').eq('organization_id', organization_id).eq('phone', phone_number).eq('country_code', country_code).maybe_single().execute())
        if response.data:
            contact_id = response.data['id']
            return ContactResolution(success=True, contact_id=contact_id, message="Contacto reconocido.", is_existing_contact=True)
        else:
            insert_response = await run_db(lambda: supabase_client.table('contacts').insert({
                'organization_id': organization_id, 'phone': phone_number, 'country_code': country_code,
                'first_name': first_name or "Nuevo", 'last_name': last_name or "Contacto",
            }).execute())
            if not insert_response or not getattr(insert_response, 'data', None):
                return ContactResolution(success=False, message="No fue posible crear el contacto")
            new_row = insert_response.data[0] if isinstance(insert_response.data, list) else insert_response.data
            new_contact_id = new_row['id']
            return ContactResolution(success=True, contact_id=new_contact_id, message="Nuevo contacto creado.", is_existing_contact=False)
    except Exception as e:
        return ContactResolution(success=False, message=f"Error al resolver contacto: {e}")

def _to_datetime(the_date: date, time_str: str):
    if not time_str: return None
    for fmt in ('%H:%M:%S', '%H:%M'):
        try: return datetime.combine(the_date, datetime.strptime(time_str, fmt).time())
        except ValueError: pass
    raise ValueError(f"Formato de hora '{time_str}' no es válido.")

@tool
async def check_availability(service_id: str, organization_id: str, check_date_str: str) -> List[AvailabilitySlot]:
    """Verifica la disponibilidad de horarios para un servicio en una fecha específica."""
    try:
        print(f"[check_availability] ▶️ Inicio | service_id={service_id}, organization_id={organization_id}, date={check_date_str}")
        check_date = datetime.strptime(check_date_str, "%Y-%m-%d").date()
        day_of_week = check_date.isoweekday()
    except ValueError:
        print(f"[check_availability] ⚠️ Fecha inválida: {check_date_str}")
        return []

    try:
        service_resp = await run_db(lambda: supabase_client.table('services').select('duration_minutes').eq('id', service_id).single().execute())
        if not service_resp:
            print("[check_availability] ❌ service_resp es None")
            return []
        if not getattr(service_resp, 'data', None):
            print(f"[check_availability] ⚠️ Servicio no encontrado para id={service_id}")
            return []
        duration = service_resp.data.get('duration_minutes')
        if not duration:
            print(f"[check_availability] ⚠️ Servicio sin duración: {service_resp.data}")
            return []

        assign_resp = await run_db(lambda: supabase_client.table('service_assignments').select('member_id').eq('service_id', service_id).execute())
        if not assign_resp or not getattr(assign_resp, 'data', None):
            print(f"[check_availability] ⚠️ Sin asignaciones de miembros para service_id={service_id}")
            return []
        member_ids = [a.get('member_id') for a in assign_resp.data if a.get('member_id')]
        print(f"[check_availability] Miembros asignados: {len(member_ids)} -> {member_ids}")
        if not member_ids: return []

        appointments_resp = await run_db(lambda: supabase_client.table('appointments').select('member_id, start_time, end_time').eq('appointment_date', check_date_str).in_('member_id', member_ids).in_('status', ['programada', 'confirmada']).execute())
        if not appointments_resp:
            print("[check_availability] ⚠️ appointments_resp es None")
        else:
            print(f"[check_availability] Citas existentes el {check_date_str}: {len(appointments_resp.data or [])}")
        booked_slots_by_member = {}
        for slot in (appointments_resp.data or []):
            mem_id = slot['member_id']
            if mem_id not in booked_slots_by_member: booked_slots_by_member[mem_id] = []
            try: booked_slots_by_member[mem_id].append((_to_datetime(check_date, slot['start_time']), _to_datetime(check_date, slot['end_time'])))
            except ValueError: continue

        org_special_date_resp = await run_db(lambda: supabase_client.table('organization_special_dates').select('*').eq('organization_id', organization_id).eq('date', check_date_str).maybe_single().execute())
        org_working_intervals = []
        if org_special_date_resp and getattr(org_special_date_resp, 'data', None):
            org_avail = org_special_date_resp.data
            if not org_avail.get('is_available'): return []
            start, end = _to_datetime(check_date, org_avail['start_time']), _to_datetime(check_date, org_avail['end_time'])
            b_start, b_end = _to_datetime(check_date, org_avail.get('break_start_time')), _to_datetime(check_date, org_avail.get('break_end_time'))
            if b_start and b_end: org_working_intervals.extend(iv for iv in [(start, b_start), (b_end, end)] if iv[0] and iv[1] and iv[0] < iv[1])
            elif start and end: org_working_intervals.append((start, end))
        else:
            org_general_avail_resp = await run_db(lambda: supabase_client.table('organization_availability').select('*').eq('organization_id', organization_id).eq('day_of_week', day_of_week).maybe_single().execute())
            if not org_general_avail_resp or not getattr(org_general_avail_resp, 'data', None):
                print(f"[check_availability] ⚠️ Sin disponibilidad general para org={organization_id} día={day_of_week}")
                return []
            if not org_general_avail_resp.data.get('is_available'):
                print(f"[check_availability] ⚠️ Organización no disponible en día={day_of_week}")
                return []
            org_avail = org_general_avail_resp.data
            start, end = _to_datetime(check_date, org_avail['start_time']), _to_datetime(check_date, org_avail['end_time'])
            b_start, b_end = _to_datetime(check_date, org_avail.get('break_start_time')), _to_datetime(check_date, org_avail.get('break_end_time'))
            if b_start and b_end: org_working_intervals.extend(iv for iv in [(start, b_start), (b_end, end)] if iv[0] and iv[1] and iv[0] < iv[1])
            elif start and end: org_working_intervals.append((start, end))
        
        if not org_working_intervals: return []

        all_final_slots = []
        member_avail_resp = await run_db(lambda: supabase_client.table('member_availability').select('*').in_('member_id', member_ids).eq('day_of_week', day_of_week).execute())
        member_special_dates_resp = await run_db(lambda: supabase_client.table('member_special_dates').select('*').in_('member_id', member_ids).eq('date', check_date_str).execute())
        print(f"[check_availability] Disponibilidad general miembros: {len(member_avail_resp.data or []) if member_avail_resp else 0}")
        print(f"[check_availability] Fechas especiales miembros: {len(member_special_dates_resp.data or []) if member_special_dates_resp else 0}")
        member_avail_map = {m['member_id']: m for m in (member_avail_resp.data or [])}
        member_special_map = {m['member_id']: m for m in (member_special_dates_resp.data or [])}

        for member_id in member_ids:
            member_working_intervals = []
            if member_id in member_special_map:
                special_day = member_special_map[member_id]
                if not special_day.get('is_available'): continue
                start, end = _to_datetime(check_date, special_day['start_time']), _to_datetime(check_date, special_day['end_time'])
                b_start, b_end = _to_datetime(check_date, special_day.get('break_start_time')), _to_datetime(check_date, special_day.get('break_end_time'))
                if b_start and b_end: member_working_intervals.extend(iv for iv in [(start, b_start), (b_end, end)] if iv[0] and iv[1] and iv[0] < iv[1])
                elif start and end: member_working_intervals.append((start, end))
            elif member_id in member_avail_map:
                general_avail = member_avail_map[member_id]
                if not general_avail.get('is_available'): continue
                start, end = _to_datetime(check_date, general_avail['start_time']), _to_datetime(check_date, general_avail['end_time'])
                b_start, b_end = _to_datetime(check_date, general_avail.get('break_start_time')), _to_datetime(check_date, general_avail.get('break_end_time'))
                if b_start and b_end: member_working_intervals.extend(iv for iv in [(start, b_start), (b_end, end)] if iv[0] and iv[1] and iv[0] < iv[1])
                elif start and end: member_working_intervals.append((start, end))
            
            real_work_intervals = []
            for mem_start, mem_end in member_working_intervals:
                for org_start, org_end in org_working_intervals:
                    overlap_start, overlap_end = max(mem_start, org_start), min(mem_end, org_end)
                    if overlap_start < overlap_end: real_work_intervals.append((overlap_start, overlap_end))
            
            free_intervals = real_work_intervals
            if member_id in booked_slots_by_member:
                for booked_start, booked_end in booked_slots_by_member[member_id]:
                    new_free_intervals = []
                    for free_start, free_end in free_intervals:
                        if booked_end <= free_start or booked_start >= free_end: new_free_intervals.append((free_start, free_end)); continue
                        if free_start < booked_start: new_free_intervals.append((free_start, booked_start))
                        if free_end > booked_end: new_free_intervals.append((booked_end, free_end))
                    free_intervals = new_free_intervals
            
            for free_start, free_end in free_intervals:
                current_time = free_start
                while current_time + timedelta(minutes=duration) <= free_end:
                    slot_end = current_time + timedelta(minutes=duration)
                    all_final_slots.append(AvailabilitySlot(start_time=current_time.strftime('%H:%M'), end_time=slot_end.strftime('%H:%M'), member_id=UUID(member_id)))
                    current_time += timedelta(minutes=15)
        
        if all_final_slots:
            from collections import Counter
            member_slot_count = Counter(slot.member_id for slot in all_final_slots)
            best_member = member_slot_count.most_common(1)[0][0]
            result = sorted([s for s in all_final_slots if s.member_id == best_member], key=lambda x: datetime.strptime(x.start_time, '%H:%M'))
            print(f"[check_availability] ✅ Slots calculados para member={best_member}: {len(result)}")
            return result
        print("[check_availability] ⚠️ Sin slots luego de combinar org/miembro/citas")
        return []
    except Exception as e:
        import traceback
        print(f"❌ Error en check_availability: {e}")
        traceback.print_exc()
        return []

@tool
async def book_appointment(organization_id: str, contact_id: str, service_id: str, member_id: str, appointment_date: str, start_time: str) -> AppointmentConfirmation:
    """Crea una cita en la base de datos."""
    try:
        print(f"[book_appointment] ▶️ Inicio | org={organization_id}, contact_id={contact_id}, service_id={service_id}, member_id={member_id}, date={appointment_date}, time={start_time}")
        if not is_valid_uuid(organization_id):
            return AppointmentConfirmation(success=False, message=f"organization_id inválido: {organization_id}")
        service_response = await run_db(lambda: supabase_client.table('services').select('duration_minutes').eq('id', service_id).single().execute())
        if not service_response or not getattr(service_response, 'data', None):
            print(f"[book_appointment] ❌ Servicio no encontrado para id={service_id}")
            return AppointmentConfirmation(success=False, message="No pude encontrar el servicio para agendar.")
        duration_minutes = service_response.data['duration_minutes']
        print(f"[book_appointment] ⏱️ Duración del servicio: {duration_minutes} minutos")
        start_datetime = datetime.fromisoformat(f"{appointment_date}T{start_time}")
        end_datetime = start_datetime + timedelta(minutes=duration_minutes)
        print(f"[book_appointment] 🕒 Rango calculado: {start_datetime.strftime('%Y-%m-%d %H:%M:%S')} → {end_datetime.strftime('%H:%M:%S')}")
        appointment_data = {
            "organization_id": organization_id,
            "contact_id": contact_id, "service_id": service_id, "member_id": str(member_id),
            "appointment_date": appointment_date, "start_time": start_datetime.strftime('%H:%M:%S'),
            "end_time": end_datetime.strftime('%H:%M:%S'), "status": "programada", "created_by": str(member_id),
        }
        print(f"[book_appointment] 📝 Datos a insertar: {appointment_data}")
        response = await run_db(lambda: supabase_client.table('appointments').insert(appointment_data).execute())
        if not response or not getattr(response, 'data', None):
            print("[book_appointment] ❌ Insert no devolvió datos")
            return AppointmentConfirmation(success=False, message="No pude confirmar la creación de la cita.")
        inserted_row = response.data[0] if isinstance(response.data, list) else response.data
        appointment_id = inserted_row['id']
        print(f"[book_appointment] ✅ Cita creada con id={appointment_id}")
        
        auth_response = await run_db(lambda: supabase_client.table('contact_authorizations').select('authorization_type').eq('contact_id', contact_id).eq('channel', 'whatsapp').order('created_at', desc=True).limit(1).maybe_single().execute())
        opt_in_status = auth_response.data['authorization_type'] if auth_response.data else "not_set"
        print(f"[book_appointment] 🔐 WhatsApp opt-in status: {opt_in_status}")
        
        return AppointmentConfirmation(success=True, appointment_id=UUID(appointment_id), opt_in_status=opt_in_status, message=f"Cita agendada con éxito para el {appointment_date} a las {start_time}.")
    except Exception as e:
        import traceback
        print(f"❌ Error en book_appointment: {e}")
        traceback.print_exc()
        return AppointmentConfirmation(success=False, message=f"Error al agendar la cita: {e}")

@tool
async def create_whatsapp_opt_in(organization_id: str, contact_id: str) -> Dict[str, Any]:
    """Crea un registro de autorización (opt-in) para WhatsApp."""
    try:
        await run_db(lambda: supabase_client.table('contact_authorizations').update({'is_active': False}).eq('contact_id', contact_id).eq('channel', 'whatsapp').execute())
        opt_in_data = {'contact_id': contact_id, 'organization_id': organization_id, 'authorization_type': 'opt_in', 'channel': 'whatsapp', 'is_active': True, 'created_by': 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11'}
        await run_db(lambda: supabase_client.table('contact_authorizations').insert(opt_in_data).execute())
        return {"success": True, "message": "Preferencia de notificaciones guardada."}
    except Exception as e:
        return {"success": False, "message": f"Error al guardar la preferencia: {e}"}

@tool
async def get_user_appointments(contact_id: str) -> List[AppointmentInfo]:
    """Consulta y devuelve las citas futuras de un usuario."""
    try:
        today = date.today().isoformat()
        response = await run_db(lambda: supabase_client.table('appointments').select('id, appointment_date, start_time, services(name), profiles(first_name, last_name)').eq('contact_id', contact_id).gte('appointment_date', today).in_('status', ['programada', 'confirmada']).order('appointment_date').order('start_time').execute())
        if not response.data: return []
        return [AppointmentInfo(appointment_id=UUID(a['id']), summary=f"Cita para '{a.get('services', {}).get('name', '')}' con {a.get('profiles', {}).get('first_name', '')} el {a['appointment_date']} a las {a['start_time']}") for a in response.data]
    except Exception as e:
        print(f"Error al obtener las citas del usuario: {e}")
        return []

@tool
async def get_user_appointments_on_date(contact_id: str, date_str: str) -> List[AppointmentInfo]:
    """Devuelve las citas del usuario para una fecha específica (programadas o confirmadas)."""
    try:
        response = await run_db(lambda: supabase_client
                                .table('appointments')
                                .select('id, appointment_date, start_time, services(name), profiles(first_name, last_name)')
                                .eq('contact_id', contact_id)
                                .eq('appointment_date', date_str)
                                .in_('status', ['programada', 'confirmada'])
                                .order('start_time')
                                .execute())
        if not response or not getattr(response, 'data', None):
            return []
        return [
            AppointmentInfo(
                appointment_id=UUID(a['id']),
                summary=f"Cita para '{a.get('services', {}).get('name', '')}' el {a['appointment_date']} a las {a['start_time']}"
            )
            for a in response.data
        ]
    except Exception as e:
        print(f"Error al obtener citas por fecha: {e}")
        return []

@tool
async def get_upcoming_user_appointments(contact_id: str, timezone: str = "America/Bogota") -> List[AppointmentInfo]:
    """Devuelve las próximas citas del usuario desde la fecha/hora actual (programadas o confirmadas)."""
    try:
        tz = pytz.timezone(timezone)
        now = datetime.now(tz)
        today = now.date().isoformat()
        now_time = now.strftime('%H:%M:%S')

        # Citas futuras (fecha > hoy)
        fut_resp = await run_db(lambda: supabase_client
                                .table('appointments')
                                .select('id, appointment_date, start_time, services(name), profiles(first_name, last_name)')
                                .eq('contact_id', contact_id)
                                .gt('appointment_date', today)
                                .in_('status', ['programada', 'confirmada'])
                                .order('appointment_date')
                                .order('start_time')
                                .execute())
        future_list = fut_resp.data or []

        # Citas de hoy con hora >= ahora
        today_resp = await run_db(lambda: supabase_client
                                  .table('appointments')
                                  .select('id, appointment_date, start_time, services(name), profiles(first_name, last_name)')
                                  .eq('contact_id', contact_id)
                                  .eq('appointment_date', today)
                                  .gte('start_time', now_time)
                                  .in_('status', ['programada', 'confirmada'])
                                  .order('start_time')
                                  .execute())
        today_list = today_resp.data or []

        merged = today_list + future_list
        return [
            AppointmentInfo(
                appointment_id=UUID(a['id']),
                summary=f"Cita para '{a.get('services', {}).get('name', '')}' el {a['appointment_date']} a las {a['start_time']}"
            ) for a in merged
        ]
    except Exception as e:
        print(f"Error al obtener próximas citas: {e}")
        return []

@tool
async def find_appointment_for_cancellation(contact_id: str, date_str: str, time_str: Optional[str] = None) -> Dict[str, Any]:
    """Encuentra la cita a cancelar por fecha (y hora opcional). Devuelve id o lista para desambiguar."""
    try:
        base = supabase_client.table('appointments') \
            .select('id, appointment_date, start_time, services(name)') \
            .eq('contact_id', contact_id) \
            .eq('appointment_date', date_str) \
            .in_('status', ['programada', 'confirmada'])
        if time_str:
            # Normalizar HH:MM o HH:MM:SS
            try:
                parsed = _to_datetime(date.fromisoformat(date_str), time_str)
                norm = parsed.strftime('%H:%M:%S')
            except Exception:
                norm = time_str if len(time_str) == 8 else f"{time_str}:00"
            base = base.eq('start_time', norm)
        resp = await run_db(lambda: base.execute())
        rows = resp.data or []
        if len(rows) == 0:
            return {"success": False, "message": "No encontré una cita que coincida."}
        if len(rows) == 1:
            a = rows[0]
            return {"success": True, "appointment_id": a['id'], "summary": f"{a.get('services', {}).get('name', '')} {a['appointment_date']} {a['start_time']}"}
        return {
            "success": True,
            "candidates": [
                {"id": a['id'], "date": a['appointment_date'], "time": a['start_time'], "service": a.get('services', {}).get('name', '')}
                for a in rows
            ],
            "message": "Se encontraron múltiples citas; especifica la hora exacta."
        }
    except Exception as e:
        return {"success": False, "message": f"Error buscando cita: {e}"}

@tool
async def find_appointment_for_update(contact_id: str, date_str: str, time_str: Optional[str] = None) -> Dict[str, Any]:
    """Encuentra la cita a actualizar (confirmar/reagendar) por fecha (y hora opcional)."""
    try:
        base = supabase_client.table('appointments') \
            .select('id, appointment_date, start_time, services(name), member_id, service_id') \
            .eq('contact_id', contact_id) \
            .eq('appointment_date', date_str) \
            .in_('status', ['programada', 'confirmada'])
        if time_str:
            try:
                parsed = _to_datetime(date.fromisoformat(date_str), time_str)
                norm = parsed.strftime('%H:%M:%S')
            except Exception:
                norm = time_str if len(time_str) == 8 else f"{time_str}:00"
            base = base.eq('start_time', norm)
        resp = await run_db(lambda: base.execute())
        rows = resp.data or []
        if len(rows) == 0:
            return {"success": False, "message": "No encontré una cita que coincida."}
        if len(rows) == 1:
            a = rows[0]
            return {
                "success": True,
                "appointment_id": a['id'],
                "summary": f"{a.get('services', {}).get('name', '')} {a['appointment_date']} {a['start_time']}",
                "service_id": a.get('service_id'),
                "member_id": a.get('member_id'),
            }
        return {
            "success": True,
            "candidates": [
                {"id": a['id'], "date": a['appointment_date'], "time": a['start_time'], "service": a.get('services', {}).get('name', '')}
                for a in rows
            ],
            "message": "Se encontraron múltiples citas; especifica la hora exacta."
        }
    except Exception as e:
        return {"success": False, "message": f"Error buscando cita (update): {e}"}

@tool
async def confirm_appointment(appointment_id: str) -> AppointmentConfirmation:
    """Confirma una cita (status = 'confirmada')."""
    try:
        print(f"[confirm_appointment] ▶️ Confirmando cita id={appointment_id}")
        await run_db(lambda: supabase_client.table('appointments').update({'status': 'confirmada'}).eq('id', appointment_id).execute())
        return AppointmentConfirmation(success=True, appointment_id=UUID(appointment_id), message="Cita confirmada.")
    except Exception as e:
        print(f"[confirm_appointment] ❌ Error: {e}")
        return AppointmentConfirmation(success=False, message=f"No pude confirmar la cita: {e}")

@tool
async def reschedule_appointment(appointment_id: str, new_date: str, new_start_time: str, member_id: str, comment: Optional[str] = None) -> AppointmentConfirmation:
    """Reagenda una cita existente: cambia fecha/hora (y miembro) y agrega una línea en `notes`.

    - Calcula automáticamente `end_time` usando la duración del servicio asociado a la cita.
    - Concatena siempre en el campo `notes` (no usa `comments`).
    """
    try:
        print(f"[reschedule_appointment] ▶️ Inicio | id={appointment_id}, new_date={new_date}, new_start={new_start_time}, member={member_id}")
        # 1) Obtener cita actual (servicio, comentarios/notas existentes)
        appt_resp = await run_db(lambda: supabase_client.table('appointments').select('*').eq('id', appointment_id).single().execute())
        if not appt_resp or not getattr(appt_resp, 'data', None):
            return AppointmentConfirmation(success=False, message="No encontré la cita a reagendar.")
        appt = appt_resp.data
        service_id = appt.get('service_id')
        old_date = appt.get('appointment_date')
        old_time = appt.get('start_time')
        existing_notes = appt.get('notes') or ""

        # 2) Duración del servicio
        svc_resp = await run_db(lambda: supabase_client.table('services').select('duration_minutes').eq('id', service_id).single().execute())
        if not svc_resp or not getattr(svc_resp, 'data', None):
            return AppointmentConfirmation(success=False, message="No pude obtener la duración del servicio.")
        duration = svc_resp.data.get('duration_minutes') or 0
        # Normalizar hora inicio y calcular fin
        try:
            start_dt = _to_datetime(date.fromisoformat(new_date), new_start_time)
        except Exception:
            # Formato flexible HH:MM
            if len(new_start_time) == 5:
                start_dt = _to_datetime(date.fromisoformat(new_date), f"{new_start_time}:00")
            else:
                raise
        end_dt = start_dt + timedelta(minutes=duration)

        # 3) Notas acumulativas (siempre en `notes`)
        user_part = f" | Nota: {comment}" if comment else ""
        comment_line = f"Reagendado de {old_date} {old_time} a {new_date} {start_dt.strftime('%H:%M:%S')}{user_part}"
        new_notes = (existing_notes + '\n' + comment_line).strip() if existing_notes else comment_line
        update_payload = {
            'appointment_date': new_date,
            'start_time': start_dt.strftime('%H:%M:%S'),
            'end_time': end_dt.strftime('%H:%M:%S'),
            'member_id': str(member_id),
            'notes': new_notes,
        }

        # 4) Actualizar
        await run_db(lambda: supabase_client.table('appointments').update(update_payload).eq('id', appointment_id).execute())
        print(f"[reschedule_appointment] ✅ Reagendado | id={appointment_id} -> {new_date} {start_dt.strftime('%H:%M:%S')} member={member_id}")
        return AppointmentConfirmation(success=True, appointment_id=UUID(appointment_id), message="Cita reagendada con éxito.")
    except Exception as e:
        import traceback
        print(f"[reschedule_appointment] ❌ Error: {e}")
        traceback.print_exc()
        return AppointmentConfirmation(success=False, message=f"No pude reagendar la cita: {e}")

@tool
async def cancel_appointment(appointment_id: str) -> AppointmentConfirmation:
    """Cancela una cita actualizando su estado a 'cancelled'."""
    try:
        await run_db(lambda: supabase_client.table('appointments').update({'status': 'cancelled'}).eq('id', appointment_id).execute())
        return AppointmentConfirmation(success=True, message="Tu cita ha sido cancelada con éxito.")
    except Exception:
        return AppointmentConfirmation(success=False, message="Lo siento, no pude cancelar tu cita.")

@tool
async def escalate_to_human(chat_identity_id: str, reason: str) -> Dict[str, Any]:
    """Marca la conversación para escalamiento a un agente humano."""
    print(f"--- 🚩 ESCALAMIENTO A HUMANO --- Razón: {reason}")
    return {"success": True, "message": "Un asesor humano será notificado."}

all_tools = [
    knowledge_search,
    update_service_in_state,
    reset_appointment_context,
    select_appointment_slot,
    resolve_relative_date,
    get_user_appointments_on_date,
    get_upcoming_user_appointments,
    find_appointment_for_cancellation,
    find_appointment_for_update,
    confirm_appointment,
    resolve_contact_on_booking,
    check_availability,
    book_appointment,
    create_whatsapp_opt_in,
    get_user_appointments,
    cancel_appointment,
    escalate_to_human,
]
