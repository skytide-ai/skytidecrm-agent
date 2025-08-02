const express = require('express');
const router = express.Router();
const axios = require('axios');
const { resolveOrganization, resolveChatIdentity } = require('../middlewares/auth');
const { createClient } = require('@supabase/supabase-js');
const { processMedia } = require('../utils/mediaProcessor');
const { sendTextMessage } = require('../utils/gupshupApi');

// Cliente con service key para operaciones de webhook (insert privilegiadas)
const supabase = createClient(
  process.env.SUPABASE_URL,
  process.env.SUPABASE_SERVICE_ROLE_KEY
);

// POST /webhooks/gupshup
// 1. Se ejecuta el middleware 'resolveOrganization'
// 2. Se ejecuta el middleware 'resolveChatIdentity' 
// 3. Se procesa el contenido del mensaje (texto o media)
// 4. Se guarda el mensaje entrante
// 5. Se ejecuta la lógica de la ruta para hacer el proxy
// 6. Se guarda el mensaje saliente
router.post('/gupshup', (req, res, next) => {
  // 🔍 DEBUG: Log completo del payload de Gupshup para analizar la estructura
  console.log('🔍 GUPSHUP PAYLOAD:', JSON.stringify(req.body, null, 2));
  
  // 🔧 MANEJAR EVENTOS DE CONFIGURACIÓN/VERIFICACIÓN DE GUPSHUP
  if (req.body.type === 'user-event' && req.body.payload?.type === 'sandbox-start') {
    console.log('✅ Gupshup webhook configuration event - responding with 200');
    return res.status(200).json({ 
      status: 'success', 
      message: 'Webhook configured successfully' 
    });
  }
  
  // Para otros tipos de eventos que no son mensajes, también responder 200
  if (req.body.type !== 'message') {
    console.log(`✅ Gupshup non-message event (${req.body.type}) - responding with 200`);
    return res.status(200).json({ 
      status: 'success', 
      message: `Event ${req.body.type} received` 
    });
  }
  
  // 🔧 ANTI-LOOP: Ignorar webhooks que son ecos de nuestros propios mensajes
  // Comparamos el `source` (quién envía) con el número de negocio de WhatsApp
  if (req.body?.payload?.source === req.whatsappBusinessNumber) {
    console.log('✅ Ignorando eco de mensaje propio del bot.');
    return res.status(200).json({ status: 'success', message: 'Bot message echo ignored' });
  }

  // Si es un mensaje real, continuar con el procesamiento normal
  next();
}, resolveOrganization, resolveChatIdentity, async (req, res) => {
  try {
    // Los middlewares ya resolvieron todo lo necesario:
    // req.organizationId, req.chatIdentityId, req.contactId, req.phone, req.countryCode, req.phoneNumber

    // Extraer información del mensaje desde Gupshup
    const messageType = req.body?.payload?.type || 'text';
    let messageContent = '';
    let mediaData = null;

    console.log(`Processing ${messageType} message for chat_identity: ${req.chatIdentityId}`);

    // 1. PROCESAR CONTENIDO SEGÚN TIPO DE MENSAJE
    if (messageType === 'text') {
      // Mensaje de texto simple
      messageContent = req.body?.payload?.payload?.text || 
                      req.body?.payload?.payload?.caption || 
                      req.body?.message || 
                      "";
    } else {
      // Mensaje con media (audio, imagen, video, documento, etc.)
      console.log('🔄 Procesando mensaje con media...');
      
      try {
        // Procesar media con Gemini (si es audio/imagen) o guardar (si es otro tipo)
        const mediaResult = await processMedia(
          req.body?.payload?.payload,  // Info del media desde Gupshup
          req.organizationId,
          req.chatIdentityId
        );

        if (mediaResult) {
          messageContent = mediaResult.processedText;
          mediaData = {
            mediaType: mediaResult.mediaType,
            mediaUrl: mediaResult.mediaUrl,
            mimeType: mediaResult.mimeType
          };
          console.log(`✅ Media procesado: ${mediaResult.mediaType}`);
        } else {
          messageContent = '[Mensaje multimedia recibido]';
          console.log('⚠️ No se pudo procesar el media');
        }
      } catch (error) {
        console.error('❌ Error procesando media:', error);
        messageContent = '[Mensaje multimedia - Error en procesamiento]';
      }
    }
    
    if (!messageContent.trim()) {
      console.error('Message content not found or processed');
      return res.status(400).json({ error: 'Message content not found in payload' });
    }

    console.log(`📝 Contenido final del mensaje: "${messageContent}"`);

    // 2. GUARDAR MENSAJE ENTRANTE (con información de media si aplica)
                const incomingMessageData = {
              chat_identity_id: req.chatIdentityId,
              direction: 'incoming',
              message: messageContent,
              message_status: 'sent',  // ⭐ Los mensajes entrantes llegan ya 'sent'
              platform_message_id: req.body?.payload?.id,
              received_via: 'whatsapp',
              organization_id: req.organizationId
            };

    // Agregar campos de media si existen
    if (mediaData) {
      incomingMessageData.media_type = mediaData.mediaType;
      incomingMessageData.media_url = mediaData.mediaUrl;
      incomingMessageData.media_mime_type = mediaData.mimeType;
    }

    const { error: incomingMessageError } = await supabase
      .from('chat_messages')
      .insert(incomingMessageData);

    if (incomingMessageError) {
      console.error('Error saving incoming message:', incomingMessageError);
      // No fallar la request por esto, solo loguearlo
    } else {
      console.log('✅ Incoming message saved to chat_messages');
    }

    // 3. CONSTRUIR PAYLOAD OPTIMIZADO PARA PYTHON SERVICE
    const pythonServiceUrl = `${process.env.PYTHON_API_URL}/invoke`;
    
    const payload = {
      organizationId: req.organizationId,
      chatIdentityId: req.chatIdentityId,      // ⭐ Ya resuelto
      contactId: req.contactId,               // ⭐ Ya resuelto  
      phone: req.phone,                       // Número completo (platform_user_id)
      countryCode: req.countryCode,           // Código de país (con + agregado)
      phoneNumber: req.phoneNumber,           // Número nacional
      message: messageContent,                // ✅ Contenido procesado (texto o transcripción/descripción)
    };

    console.log(`Forwarding processed payload to Python service:`, JSON.stringify(payload, null, 2));

    // 4. ENVIAR AL PYTHON SERVICE
    const responseFromPython = await axios.post(pythonServiceUrl, payload);
    
    console.log('Received response from Python service:');
    console.log(JSON.stringify(responseFromPython.data, null, 2));

    // 5. EXTRAER RESPUESTA DEL AGENTE
    const aiResponse = responseFromPython.data?.response || responseFromPython.data?.message || 'Sin respuesta';
    
    // 6. ENVIAR RESPUESTA A WHATSAPP VIA GUPSHUP
    console.log('🚀 Enviando respuesta a WhatsApp via Gupshup...');
    const gupshupResult = await sendTextMessage(
      req.gupshupApiKey,
      req.whatsappBusinessNumber,
      req.phone, // Número del usuario que envió el mensaje
      aiResponse
    );

    let messageStatus = 'pending';
    if (gupshupResult.success) {
      messageStatus = 'sent';
      console.log(`✅ Mensaje enviado a WhatsApp. MessageId: ${gupshupResult.messageId}`);
    } else {
      console.error('❌ Error enviando mensaje a WhatsApp:', gupshupResult.error);
      messageStatus = 'failed';
    }
    
    // 7. GUARDAR MENSAJE SALIENTE CON ESTADO CORRECTO
    const { data: outgoingMessage, error: outgoingMessageError } = await supabase
      .from('chat_messages')
      .insert({
        chat_identity_id: req.chatIdentityId,
        direction: 'outgoing',
        message: aiResponse,
        message_status: messageStatus, // 'sent', 'failed', o 'pending'
        received_via: 'whatsapp',
        organization_id: req.organizationId,
        // Agregar messageId de Gupshup si está disponible
        ...(gupshupResult.success && { platform_message_id: gupshupResult.messageId })
      })
      .select('id')
      .single();

    if (outgoingMessageError) {
      console.error('Error saving outgoing message:', outgoingMessageError);
      // No fallar la request por esto, solo loguearlo
    } else {
      console.log(`✅ Outgoing message saved with status: ${messageStatus}`);
    }

    // 8. RESPONDER A GUPSHUP
    // Gupshup espera una respuesta HTTP 200 para confirmar que recibimos el webhook
    res.status(200).json({ 
      status: 'processed', 
      message: 'Message processed and response sent',
      gupshupMessageId: gupshupResult.success ? gupshupResult.messageId : null
    });

  } catch (error) {
    console.error('Error in webhook processing:', error.message);
    if (error.response) {
      // El servidor de Python respondió con un código de error
      console.error('Python service response:', error.response.data);
      res.status(error.response.status).json(error.response.data);
    } else if (error.request) {
      // La petición fue hecha pero no se recibió respuesta
      console.error('No response received from Python service');
      res.status(503).json({ error: 'Service unavailable: No response from AI service.' });
    } else {
      // Ocurrió un error al configurar la petición
      res.status(500).json({ error: 'Internal Server Error in Gateway' });
    }
  }
});

module.exports = router; 