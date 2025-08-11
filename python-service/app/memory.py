from typing import List, Optional, Dict, Any
from .db import supabase_client, run_db


async def get_last_messages(chat_identity_id: str, last_n: int = 3) -> List[Dict[str, Any]]:
    """Devuelve los últimos N mensajes de un hilo, ordenados de más antiguo a más reciente.
    Estructura: [{ role: 'user'|'assistant', content: str }]
    """
    try:
        resp = await run_db(
            lambda: supabase_client
            .table('chat_messages')
            .select('direction, message, processed_text, timestamp')
            .eq('chat_identity_id', chat_identity_id)
            .order('timestamp', desc=True)
            .limit(last_n)
            .execute()
        )
        rows = resp.data or []
        rows.reverse()
        result: List[Dict[str, Any]] = []
        for r in rows:
            role = 'user' if r.get('direction') == 'incoming' else 'assistant'
            content = r.get('processed_text') or r.get('message') or ''
            result.append({'role': role, 'content': content})
        return result
    except Exception as e:
        print(f"❌ get_last_messages error: {e}")
        return []


async def get_context_block(chat_identity_id: str) -> str:
    """Obtiene el resumen persistente del hilo (si existe)."""
    try:
        resp = await run_db(
            lambda: supabase_client
            .table('thread_summaries')
            .select('summary_text')
            .eq('chat_identity_id', chat_identity_id)
            .maybe_single()
            .execute()
        )
        if not resp or not getattr(resp, 'data', None):
            return "No hay resumen."
        return resp.data.get('summary_text') or "No hay resumen."
    except Exception as e:
        print(f"❌ get_context_block error: {e}")
        return "No hay resumen."


async def upsert_thread_summary(organization_id: str, chat_identity_id: str, summary_text: str) -> bool:
    """Crea o actualiza el resumen del hilo."""
    try:
        payload = {
            'organization_id': organization_id,
            'chat_identity_id': chat_identity_id,
            'summary_text': summary_text,
        }
        # Intentar update por pk compuesta lógica; si no existe, insertar
        resp = await run_db(
            lambda: supabase_client
            .table('thread_summaries')
            .upsert(payload, on_conflict='chat_identity_id')
            .execute()
        )
        return bool(resp)
    except Exception as e:
        print(f"❌ upsert_thread_summary error: {e}")
        return False


