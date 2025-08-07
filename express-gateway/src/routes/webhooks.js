const express = require('express');
const router = express.Router();
const axios = require('axios');
const { createClient } = require('@supabase/supabase-js');
const { processMedia } = require('../utils/mediaProcessor');
const { sendTextMessage } = require('../utils/gupshupApi');

// Cache para deduplicación
const processedMessages = new Map();
const DEDUP_TTL = 5 * 60 * 1000; // 5 minutos

setInterval(() => {
  const now = Date.now();
  for (const [messageId, timestamp] of processedMessages.entries()) {
    if (now - timestamp > DEDUP_TTL) {
      processedMessages.delete(messageId);
    }
  }
}, 60000);

let executionCounter = 0;

router.post('/gupshup', async (req, res) => {
  executionCounter++;
  const requestId = `${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
  console.log(`🔍 [${requestId}] WEBHOOK INICIADO - EJECUCIÓN #${executionCounter}`);

  // --- VALIDACIONES SÍNCRONAS INMEDIATAS ---
  
  // 1. Validar tipo de evento
  if (req.body.type !== 'message') {
    console.log(`✅ [${requestId}] Evento no-mensaje (${req.body.type}) ignorado.`);
    return res.status(200).json({ status: 'success', message: `Event ${req.body.type} received` });
  }

  // 2. Deduplicación inmediata con cache
  const messageId = req.body?.payload?.id;
  if (messageId && processedMessages.has(messageId)) {
    console.log(`✅ [${requestId}] Mensaje duplicado ignorado (cache). MessageId: ${messageId}`);
    return res.status(200).json({ status: 'success', message: 'Duplicate message ignored' });
  }
  
  if (messageId) {
    processedMessages.set(messageId, Date.now());
  }

  // ✅ RESPONDER 200 OK INMEDIATAMENTE A GUPSHUP
  console.log(`✅ [${requestId}] Confirmando recepción a Gupshup ANTES de procesar.`);
  res.status(200).json({ status: 'received', message: 'Message received and will be processed' });

  // --- PROCESAMIENTO ASÍNCRONO ---
  setImmediate(async () => {
    const processingId = `${requestId}-async`;
    console.log(`🚀 [${requestId}] INICIANDO procesamiento asíncrono [${processingId}]`);
    
    try {
      const supabase = createClient(process.env.SUPABASE_URL, process.env.SUPABASE_SERVICE_ROLE_KEY);
      
      // 3. Resolver Organización
      const appName = req.body.app;
      const { data: connection, error: orgError } = await supabase
        .from('platform_connections')
        .select('organization_id, gupshup_api_key, whatsapp_business_number')
        .eq('gupshup_app_name', appName)
        .eq('is_active', true)
        .single();

      if (orgError || !connection) {
        console.error(`❌ [${processingId}] Error resolviendo organización para app: ${appName}`, orgError);
        return;
      }
      const { organization_id, gupshup_api_key, whatsapp_business_number } = connection;

      // 4. Filtro Anti-Loop
      if (req.body?.payload?.source === whatsapp_business_number) {
        console.log(`✅ [${processingId}] Eco de mensaje propio ignorado.`);
        return;
      }

      // 5. Resolver Chat Identity
      const sender = req.body?.payload?.sender;
      const phone = sender?.phone;
      const { data: identity, error: identityError } = await supabase
        .from('chat_identities')
        .select('id, contact_id')
        .eq('organization_id', organization_id)
        .eq('platform_user_id', phone)
        .single();

      if (identityError && identityError.code !== 'PGRST116') { // Ignorar 'not found'
        console.error(`❌ [${processingId}] Error buscando chat_identity:`, identityError);
        return;
      }
      
      let chatIdentityId = identity?.id;
      let contactId = identity?.contact_id;

      if (!chatIdentityId) {
        const { data: newIdentity, error: createError } = await supabase
          .from('chat_identities')
          .insert({ organization_id, platform_user_id: phone, platform: 'whatsapp' })
          .select('id')
          .single();
        if (createError) {
          console.error(`❌ [${processingId}] Error creando chat_identity:`, createError);
          return;
        }
        chatIdentityId = newIdentity.id;
      } else {
        await supabase.from('chat_identities').update({ last_seen: new Date().toISOString() }).eq('id', chatIdentityId);
      }

      // 6. Procesar Contenido del Mensaje
      let messageContent = req.body?.payload?.payload?.text || '[Mensaje multimedia]';
      console.log(`🟢 [${processingId}] MENSAJE ENTRANTE`);
      console.log(`   Org: ${organization_id}`);
      console.log(`   From: ${phone}`);
      console.log(`   Texto (${messageContent?.length || 0} chars): ${messageContent}`);
      // ... (lógica de processMedia iría aquí si se necesita)

      // 7. Guardar Mensaje Entrante
      await supabase.from('chat_messages').insert({
        chat_identity_id: chatIdentityId,
        direction: 'incoming',
        message: messageContent,
        platform_message_id: messageId,
        organization_id
      });

      // 8. Enviar a Python Service
      const pythonServiceUrl = `${process.env.PYTHON_SERVICE_URL}/invoke`;
      const payload = {
        organizationId: organization_id,
        chatIdentityId,
        contactId,
        phone,
        countryCode: `+${sender.country_code}`,
        phoneNumber: sender.dial_code,
        message: messageContent
      };
      
      const responseFromPython = await axios.post(pythonServiceUrl, payload, { timeout: 60000 });
      const aiResponse = responseFromPython.data?.response || 'Sin respuesta';
      console.log(`🔵 [${processingId}] RESPUESTA AGENTE (${aiResponse?.length || 0} chars): ${aiResponse}`);

      // 9. Enviar Respuesta a Gupshup
      const gupshupResult = await sendTextMessage(gupshup_api_key, whatsapp_business_number, phone, aiResponse);

      // 10. Guardar Mensaje Saliente
      await supabase.from('chat_messages').insert({
        chat_identity_id: chatIdentityId,
        direction: 'outgoing',
        message: aiResponse,
        message_status: gupshupResult.success ? 'sent' : 'failed',
        platform_message_id: gupshupResult.messageId,
        organization_id
      });

      console.log(`🏁 [${requestId}] COMPLETADO procesamiento asíncrono [${processingId}]`);

    } catch (error) {
      console.error(`🔥 [${processingId}] FALLO en procesamiento asíncrono:`, error.message);
    }
  });
});

module.exports = router;