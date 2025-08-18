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




