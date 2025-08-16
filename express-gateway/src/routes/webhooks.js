const express = require('express');
const router = express.Router();
const axios = require('axios');
const { createClient } = require('@supabase/supabase-js');
const { createClient: createRedisClient } = require('redis');
const { processMedia } = require('../utils/mediaProcessor');
const { sendTextMessage } = require('../utils/gupshupApi');

// Cache para deduplicación
const processedMessages = new Map();
const DEDUP_TTL = 5 * 60 * 1000; // 5 minutos

// (Eliminado) Cache de identidad/contacto: ahora siempre se consulta DB para respetar bot_enabled

// Buffer de mensajes por chat (debounce)
const pendingByChat = new Map(); // key: `${org}:${chatIdentityId}` -> { items: string[], timer: NodeJS.Timeout|null, ctx: object }
const DEBOUNCE_MS = parseInt(process.env.GATEWAY_DEBOUNCE_MS || '10000', 10);
const MAX_BATCH = 5;

// Redis: cache de últimos N mensajes por chat
const REDIS_URL = process.env.REDIS_URL;
const USE_REDIS_CACHE = !!REDIS_URL;
let redis;
if (USE_REDIS_CACHE) {
  try {
    redis = createRedisClient({ url: REDIS_URL });
    redis.on('error', (err) => console.error('Redis error:', err?.message || err));
    redis.connect().then(() => console.log('🔌 Redis conectado (gateway)')).catch((e) => console.error('Redis connect error:', e?.message || e));
  } catch (e) {
    console.warn('⚠️ No se pudo inicializar Redis en gateway. Continuando sin caché.', e?.message || e);
    redis = null;
  }
}

const REDIS_LAST_N = parseInt(process.env.REDIS_CHAT_CACHE_N || '30', 10);
const REDIS_TTL_SECONDS = parseInt(process.env.REDIS_CHAT_CACHE_TTL || '600', 10); // 10 minutos

async function cacheAppendMessage(chatKey, role, content) {
  if (!redis) return;
  const listKey = `cache:chat:${chatKey}`;
  try {
    await redis.rPush(listKey, JSON.stringify({ role, content }));
    const len = await redis.lLen(listKey);
    if (len > REDIS_LAST_N) {
      await redis.lTrim(listKey, len - REDIS_LAST_N, -1);
    }
    await redis.expire(listKey, REDIS_TTL_SECONDS);
  } catch (e) {
    console.warn('⚠️ Error cacheAppendMessage:', e?.message || e);
  }
}

async function cacheGetLastMessages(chatKey, n) {
  if (!redis) return null;
  const listKey = `cache:chat:${chatKey}`;
  try {
    const len = await redis.lLen(listKey);
    if (!len) return [];
    const start = Math.max(0, len - n);
    const raw = await redis.lRange(listKey, start, -1);
    return raw.map((x) => {
      try { return JSON.parse(x); } catch { return null; }
    }).filter(Boolean);
  } catch (e) {
    console.warn('⚠️ Error cacheGetLastMessages:', e?.message || e);
    return null;
  }
}

async function flushBuffer(chatKey) {
  const entry = pendingByChat.get(chatKey);
  if (!entry) return;
  const { items, ctx } = entry;
  pendingByChat.delete(chatKey);
  if (!items || items.length === 0) return;
  const joined = items.join('\n');
  try {
    const { pythonServiceUrl, organization_id, chatIdentityId, contactId, phone, countryCode, phoneNumber, firstName, gupshup_api_key, whatsapp_business_number, supabase } = ctx;
    // Obtener últimos N mensajes desde caché (si existe) para evitar leer de Supabase
    const recentMessages = await cacheGetLastMessages(chatKey, Math.min(REDIS_LAST_N, 24)) || [];
    const payload = {
      organizationId: String(organization_id || ''),
      chatIdentityId: String(chatIdentityId || ''),
      contactId: contactId || null,
      phone: String(phone || ''),
      countryCode: String(countryCode || ''),
      phoneNumber: String(phoneNumber || ''),
      firstName: firstName || null,
      message: String(joined || ''),
      recentMessages: Array.isArray(recentMessages)
        ? recentMessages.map(m => ({
            role: String((m && m.role) || ''),
            content: String((m && m.content) || '')
          }))
        : []
    };
    console.log(`📝 [${chatKey}] PAYLOAD invoke: ${JSON.stringify(payload)}`);
    const responseFromPython = await axios.post(pythonServiceUrl, payload, { timeout: 120000 });
    const aiResponse = responseFromPython.data?.response || 'Sin respuesta';
    console.log(`🔵 [${chatKey}] FLUSH (${items.length} msgs) → RESPUESTA (${aiResponse?.length || 0} chars)`);

    // Enviar a Gupshup
    let gupshupResult = { success: false, messageId: null };
    try {
      gupshupResult = await sendTextMessage(gupshup_api_key, whatsapp_business_number, phone, aiResponse);
    } catch (e) {
      console.error(`❌ [${chatKey}] Error enviando a Gupshup:`, e?.message || e);
    }

    // Guardar mensaje saliente tras el intento de envío:
    // - success: 'pending' (Netlify webhook actualizará a sent/delivered/read)
    // - error: 'failed'
    const { error: insertOutgoingErr } = await supabase
      .from('chat_messages')
      .insert({
        chat_identity_id: chatIdentityId,
        direction: 'outgoing',
        message: aiResponse,
        message_status: gupshupResult.success ? 'pending' : 'failed',
        platform_message_id: gupshupResult.messageId || null,
        received_via: 'whatsapp',
        organization_id
      });
    if (insertOutgoingErr) {
      console.warn(`⚠️ [${chatKey}] No se pudo insertar mensaje saliente:`, insertOutgoingErr.message || insertOutgoingErr);
    }

    // Cache: guardar respuesta del asistente
    await cacheAppendMessage(chatKey, 'assistant', aiResponse);
  } catch (e) {
    console.error(`🔥 [${chatKey}] Error en flush de buffer:`, e?.message || e);
  }
}

setInterval(() => {
  const now = Date.now();
  for (const [messageId, timestamp] of processedMessages.entries()) {
    if (now - timestamp > DEDUP_TTL) {
      processedMessages.delete(messageId);
    }
  }
  // (Eliminado) Limpieza de identityCache
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

      // 5. Resolver Chat Identity (con caché)
      const sender = req.body?.payload?.sender;
      const phone = sender?.phone;
      let chatIdentityId, contactId, firstName;
      {
        const { data: identity, error: identityError } = await supabase
          .from('chat_identities')
          .select('id, contact_id, bot_enabled')
          .eq('organization_id', organization_id)
          .eq('platform_user_id', phone)
          .single();

        if (identityError && identityError.code !== 'PGRST116') { // Ignorar 'not found'
          console.error(`❌ [${processingId}] Error buscando chat_identity:`, identityError);
          return;
        }
        
        chatIdentityId = identity?.id;
        contactId = identity?.contact_id;
        var botEnabled = identity?.bot_enabled === true;

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
          botEnabled = true; // por defecto habilitado al crear
        }

        // Si ya hay contactId, obtener first_name una sola vez
        if (contactId) {
          const { data: contact, error: contactErr } = await supabase
            .from('contacts')
            .select('first_name')
            .eq('id', contactId)
            .single();
          if (contactErr && contactErr.code !== 'PGRST116') {
            console.warn(`⚠️ [${processingId}] Error obteniendo first_name:`, contactErr?.message);
          } else {
            firstName = contact?.first_name || null;
          }
        }

      }

      // botEnabled ya resuelto en la consulta principal

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
      }

      // 6. Procesar Contenido del Mensaje (texto normalizado)
      const incoming = req.body?.payload || {};
      const payloadInner = incoming?.payload || {};
      const msgType = incoming?.type || 'text';
      let messageContent = payloadInner?.text || '[Mensaje multimedia]';
      let processedText = messageContent;
      let mediaMeta = null; // { mediaUrl, mediaType, mimeType }
      try {
        if (msgType && msgType !== 'text') {
          // Para media, necesitamos pasar el tipo correcto y la URL
          const mediaPayload = {
            type: msgType,  // 'audio', 'image', 'video', etc.
            url: payloadInner?.url || payloadInner?.payload?.url,  // La URL puede estar en diferentes lugares
            ...payloadInner  // Incluir todo el payload por si hay más datos
          };
          
          console.log(`📨 [${processingId}] Procesando media tipo ${msgType}:`, JSON.stringify(mediaPayload).substring(0, 200));
          
          const mediaResult = await processMedia(mediaPayload, organization_id, chatIdentityId);
          if (mediaResult && mediaResult.processedText) {
            processedText = mediaResult.processedText;
            mediaMeta = { mediaUrl: mediaResult.mediaUrl, mediaType: mediaResult.mediaType, mimeType: mediaResult.mimeType };
            console.log(`✅ [${processingId}] Media procesada exitosamente: ${processedText.substring(0, 100)}...`);
          } else {
            console.log(`⚠️ [${processingId}] No se pudo procesar media, usando mensaje por defecto`);
          }
        }
      } catch (e) {
        console.warn(`⚠️ [${processingId}] Error procesando media:`, e?.message || e);
      }
      console.log(`🟢 [${processingId}] MENSAJE ENTRANTE`);
      console.log(`   Org: ${organization_id}`);
      console.log(`   From: ${phone}`);
      console.log(`   Texto (${processedText?.length || 0} chars): ${processedText}`);
      // ... (lógica de processMedia iría aquí si se necesita)

      // 7. Guardar Mensaje Entrante
      // Para contact y location, guardar como text ya que no son tipos multimedia válidos en el enum
      let dbMediaType = mediaMeta?.mediaType;
      if (dbMediaType === 'contact' || dbMediaType === 'location') {
        dbMediaType = null; // Guardar como NULL en media_type ya que no es un archivo multimedia
      }
      
      const chatRow = {
        chat_identity_id: chatIdentityId,
        direction: 'incoming',
        message: msgType === 'text' ? messageContent : '',
        processed_text: processedText,
        media_type: dbMediaType || null,
        media_url: mediaMeta?.mediaUrl || null,
        media_mime_type: mediaMeta?.mimeType || null,
        platform_message_id: messageId,
        received_via: 'whatsapp',
        organization_id
      };
      {
        const { error: insertIncomingErr } = await supabase
          .from('chat_messages')
          .insert(chatRow);
        if (insertIncomingErr) {
          console.error(`❌ [${processingId}] Error guardando mensaje entrante en chat_messages:`, insertIncomingErr);
        }
      }

      // Si el bot está deshabilitado para este chat, terminar aquí (solo persistimos el mensaje)
      if (botEnabled === false) {
        console.log(`⏸️ [${processingId}] bot_enabled=false para chat_identity=${chatIdentityId}. No se invoca al agente.`);
        return;
      }

      // 7.1 Cache: agregar mensaje de usuario (contenido normalizado)
      const cacheChatKey = `${organization_id}:${chatIdentityId}`;
      await cacheAppendMessage(cacheChatKey, 'user', processedText);

      // 8. Buffer de 10s (debounce) para consolidar mensajes
      const pythonServiceUrl = `${process.env.PYTHON_SERVICE_URL}/invoke`;
      const chatKey = `${organization_id}:${chatIdentityId}`;
      const ctx = { pythonServiceUrl, organization_id, chatIdentityId, contactId, phone, countryCode: `+${sender.country_code}`, phoneNumber: sender.dial_code, firstName, gupshup_api_key, whatsapp_business_number, supabase };
      console.log(`🔎 [${processingId}] DEBUG sender: country_code=${sender?.country_code}, dial_code=${sender?.dial_code}, phone=${phone}`);
      console.log(`🔎 [${processingId}] DEBUG ctx for Python: org=${organization_id}, chatId=${chatIdentityId}, contactId=${contactId}, phone=${phone}, countryCode=${ctx.countryCode}, phoneNumber=${ctx.phoneNumber}, firstName=${firstName}`);

      let entry = pendingByChat.get(chatKey);
      if (!entry) {
        entry = { items: [], timer: null, ctx };
        pendingByChat.set(chatKey, entry);
      } else {
        entry.ctx = ctx; // actualizar contexto por si cambió algo
      }

      entry.items.push(processedText);
      if (entry.items.length >= MAX_BATCH) {
        if (entry.timer) clearTimeout(entry.timer);
        await flushBuffer(chatKey);
      } else {
        if (entry.timer) clearTimeout(entry.timer);
        entry.timer = setTimeout(() => flushBuffer(chatKey), DEBOUNCE_MS);
      }

      console.log(`🏁 [${requestId}] BUFFER actualizado [${processingId}] (${entry.items.length} msgs, debounce ${DEBOUNCE_MS}ms)`);

    } catch (error) {
      console.error(`🔥 [${processingId}] FALLO en procesamiento asíncrono:`, error.message);
    }
  });
});

module.exports = router;