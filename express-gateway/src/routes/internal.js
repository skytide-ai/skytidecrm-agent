
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
    const { organization_id, recipient_phone, customer_name, customer_phone, escalation_reason } = req.body;

    if (!organization_id || !recipient_phone || !customer_name || !customer_phone || !escalation_reason) {
        return res.status(400).json({ error: 'Faltan parámetros requeridos en el cuerpo de la solicitud.' });
    }

    try {
        // 1. Obtener las credenciales de Gupshup para la organización
        const { data: connection, error: connError } = await supabase
            .from('platform_connections')
            .select('gupshup_api_key, whatsapp_business_number')
            .eq('organization_id', organization_id)
            .single();

        if (connError || !connection) {
            console.error('Error al obtener las credenciales de Gupshup:', connError);
            return res.status(500).json({ error: 'No se pudieron obtener las credenciales para la organización.' });
        }

        const { gupshup_api_key, whatsapp_business_number } = connection;
        
        console.log(`[ESCALATION] Notificando a ${recipient_phone} desde ${whatsapp_business_number}`);

        // 2. Preparar y enviar la notificación a Gupshup
        const gupshupUrl = `https://api.gupshup.io/wa/api/v1/template/msg`;
        const templateId = '0420f88a-531f-4c3d-8893-81b08f1b1d6c'; // Template de escalación
        
        // Generar la hora actual para el parámetro {{4}}
        const currentTime = new Date().toLocaleString('es-CO', { 
            timeZone: 'America/Bogota',
            year: 'numeric',
            month: '2-digit', 
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit'
        });
        
        // Parámetros del template: {{1}} customer_name, {{2}} customer_phone, {{3}} escalation_reason, {{4}} hora
        const templateParams = [customer_name, customer_phone, escalation_reason, currentTime];

        const payload = {
            channel: 'whatsapp',
            source: whatsapp_business_number,
            destination: recipient_phone,
            template: JSON.stringify({
                id: templateId,
                params: templateParams
            }),
            'src.name': 'YourAppName' // Este puede ser el gupshup_app_name si lo obtenemos también
        };

        const headers = {
            'apikey': gupshup_api_key,
            'Content-Type': 'application/x-www-form-urlencoded'
        };

        // NOTA: La llamada real a axios está comentada para evitar efectos secundarios durante el desarrollo.
        // const response = await axios.post(gupshupUrl, new URLSearchParams(payload).toString(), { headers });
        // console.log('[ESCALATION] Respuesta de Gupshup:', response.data);

        console.log('[ESCALATION] Simulación de notificación enviada con éxito.');
        res.status(200).json({ success: true, message: 'Notificación de escalación enviada para procesar.' });

    } catch (error) {
        console.error('Error procesando la notificación de escalación:', error.message);
        res.status(500).json({ error: 'Error interno del servidor al procesar la escalación.' });
    }
});

module.exports = router;
