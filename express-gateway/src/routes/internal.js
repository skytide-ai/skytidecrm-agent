
const express = require('express');
const router = express.Router();
const axios = require('axios');
const { createClient } = require('@supabase/supabase-js');

// Configuración de Supabase
const supabaseUrl = process.env.SUPABASE_URL;
const supabaseAnonKey = process.env.SUPABASE_ANON_KEY;

if (!supabaseUrl || !supabaseAnonKey) {
    console.error('❌ Error: SUPABASE_URL y SUPABASE_ANON_KEY deben estar configuradas en las variables de entorno');
    process.exit(1);
}

const supabase = createClient(supabaseUrl, supabaseAnonKey);

/**
 * @route POST /internal/notify/escalation
 * @description Recibe una solicitud interna del servicio de Python para notificar a un humano sobre una escalación.
 * @body {string} organization_id - El ID de la organización.
 * @body {string} recipient_phone - El número de teléfono del miembro del personal a notificar.
 * @body {string} customer_name - El nombre del cliente que necesita atención.
 * @body {string} customer_phone - El teléfono del cliente que necesita atención.
 * @body {string} escalation_reason - El motivo de la escalación.
 */
router.post('/notify/escalation', async (req, res) => {
  const { organization_id, chat_identity_id, phone_number, country_code, reason } = req.body;

  if (!organization_id || !chat_identity_id || !phone_number || !country_code || !reason) {
    return res.status(400).json({ error: 'Faltan parámetros requeridos: organization_id, chat_identity_id, phone_number, country_code, reason' });
  }

  try {
    // 1) Configuración global para notificaciones (ENV)
    const notifApiKey = process.env.GUPSHUP_NOTIF_API_KEY;
    const notifSource = process.env.GUPSHUP_NOTIF_SOURCE; // número emisor (ej: +57300...)
    const notifTemplateId = process.env.GUPSHUP_ESCALATION_TEMPLATE_ID;
    const notifAppName = process.env.GUPSHUP_NOTIF_APP_NAME || 'CRM-Notifications';
    if (!notifApiKey || !notifSource || !notifTemplateId) {
      return res.status(500).json({ error: 'Variables de entorno de notificaciones no configuradas.' });
    }

    // 2) Obtener destinatario (asesor) desde internal_notifications_config
    const { data: notifCfg, error: notifErr } = await supabase
      .from('internal_notifications_config')
      .select('is_enabled, recipient_phone, country_code')
      .eq('organization_id', organization_id)
      .single();
    if (notifErr || !notifCfg || notifCfg.is_enabled !== true) {
      return res.status(400).json({ error: 'Notificaciones internas no habilitadas o sin configuración para la organización.' });
    }

    const advisorCountry = (notifCfg.country_code || '').replace('+', '');
    const advisorPhone = String(notifCfg.recipient_phone || '').replace(/\D/g, '');
    if (!advisorCountry || !advisorPhone) {
      return res.status(400).json({ error: 'Configuración de destinatario inválida.' });
    }
    const destination = `${advisorCountry}${advisorPhone}`;

    // 3) Nombre del cliente: buscar en contacts por phone_number y country_code; si no, usar el número
    const { data: contactRow } = await supabase
      .from('contacts')
      .select('first_name, last_name')
      .eq('organization_id', organization_id)
      .eq('phone', phone_number)
      .eq('country_code', country_code)
      .maybeSingle();
    const clientName = contactRow ? `${contactRow.first_name || ''} ${contactRow.last_name || ''}`.trim() || `${country_code}${phone_number}` : `${country_code}${phone_number}`;
    const clientPhoneParam = `${country_code}${phone_number}`; // con +

    // 4) Construir payload para Gupshup
    const gupshupUrl = 'https://api.gupshup.io/wa/api/v1/template/msg';
    const currentTime = new Date().toLocaleString('es-CO', { timeZone: 'America/Bogota', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
    const templateParams = [clientName, clientPhoneParam, reason, currentTime];

    const payload = {
      channel: 'whatsapp',
      source: notifSource,
      destination,
      template: JSON.stringify({ id: notifTemplateId, params: templateParams }),
      'src.name': notifAppName,
    };
    const headers = { apikey: notifApiKey, 'Content-Type': 'application/x-www-form-urlencoded' };

    // 5) Desactivar bot (chat_identities)
    const { error: upErr } = await supabase
      .from('chat_identities')
      .update({ bot_enabled: false })
      .eq('id', chat_identity_id);
    if (upErr) {
      console.warn('[ESCALATION] No se pudo desactivar bot_enabled:', upErr?.message || upErr);
    }

    // 6) Enviar a Gupshup (real)
    try {
      const response = await axios.post(gupshupUrl, new URLSearchParams(payload).toString(), { headers });
      console.log('[ESCALATION] Notificación enviada. Gupshup:', response.data);
    } catch (e) {
      console.error('[ESCALATION] Error enviando a Gupshup:', e?.response?.data || e?.message || e);
      return res.status(502).json({ error: 'Fallo enviando notificación a Gupshup' });
    }

    return res.status(200).json({ success: true });
  } catch (error) {
    console.error('Error procesando la notificación de escalación:', error.message);
    res.status(500).json({ error: 'Error interno del servidor al procesar la escalación.' });
  }
});

module.exports = router;
