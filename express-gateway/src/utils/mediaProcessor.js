const { GoogleGenerativeAI } = require('@google/generative-ai');
const axios = require('axios');
const { createClient } = require('@supabase/supabase-js');

// Configuración de Supabase
const supabaseUrl = process.env.SUPABASE_URL;
const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY;
const geminiApiKey = process.env.GEMINI_API_KEY;

if (!supabaseUrl || !supabaseServiceKey) {
    console.error('❌ Error: SUPABASE_URL y SUPABASE_SERVICE_ROLE_KEY deben estar configuradas en las variables de entorno');
    process.exit(1);
}

if (!geminiApiKey) {
    console.error('❌ Error: GEMINI_API_KEY debe estar configurada en las variables de entorno');
    process.exit(1);
}

// Usar SERVICE_ROLE_KEY para bypasear RLS en Storage
const supabase = createClient(supabaseUrl, supabaseServiceKey);
const genAI = new GoogleGenerativeAI(geminiApiKey);

/**
 * Descarga un archivo desde una URL y lo convierte a Buffer
 */
async function downloadFile(url) {
  const response = await axios.get(url, { responseType: 'arraybuffer' });
  return {
    buffer: Buffer.from(response.data),
    mimeType: response.headers['content-type']
  };
}

/**
 * Sube un archivo a Supabase Storage
 */
async function uploadToSupabaseStorage(buffer, filePath, mimeType) {
  const { data, error } = await supabase.storage
    .from('chat-media')
    .upload(filePath, buffer, {
      contentType: mimeType,
      upsert: false
    });

  if (error) {
    throw new Error(`Error uploading to Supabase Storage: ${error.message}`);
  }

  // Construir URL pública del archivo
  const publicUrl = `${process.env.SUPABASE_URL}/storage/v1/object/public/chat-media/${filePath}`;
  return publicUrl;
}

/**
 * Procesa audio usando Gemini para transcripción
 */
async function processAudio(audioUrl, organizationId, chatIdentityId) {
  try {
    console.log('📱 Procesando audio...');
    
    // 1. Descargar archivo
    const { buffer, mimeType } = await downloadFile(audioUrl);
    
    // 2. Generar nombre de archivo
    const timestamp = Date.now();
    const extension = mimeType.includes('ogg') ? 'ogg' : 'mp3';
    const filePath = `${organizationId}/${chatIdentityId}/${timestamp}.${extension}`;
    
    // 3. Subir a Supabase Storage
    const publicUrl = await uploadToSupabaseStorage(buffer, filePath, mimeType);
    
    // 4. Procesar con Gemini
    const model = genAI.getGenerativeModel({ model: "gemini-2.0-flash-exp" });
    
    const result = await model.generateContent([
      {
        inlineData: {
          data: buffer.toString('base64'),
          mimeType: mimeType
        }
      },
      "Transcribe este audio de WhatsApp a texto. Devuelve SOLO la transcripción, sin comentarios adicionales."
    ]);
    
    const transcription = result.response.text();
    
    return {
      processedText: `[Audio transcrito]: ${transcription}`,
      mediaUrl: publicUrl,
      mediaType: 'audio',
      mimeType: mimeType
    };
    
  } catch (error) {
    console.error('Error procesando audio:', error);
    return {
      processedText: '[Audio recibido - Error en transcripción]',
      mediaUrl: null,
      mediaType: 'audio',
      mimeType: 'unknown'
    };
  }
}

/**
 * Procesa imagen usando Gemini para descripción
 */
async function processImage(imageUrl, organizationId, chatIdentityId) {
  try {
    console.log('🖼️ Procesando imagen...');
    
    // 1. Descargar archivo
    const { buffer, mimeType } = await downloadFile(imageUrl);
    
    // 2. Generar nombre de archivo
    const timestamp = Date.now();
    const extension = mimeType.includes('jpeg') ? 'jpg' : 'png';
    const filePath = `${organizationId}/${chatIdentityId}/${timestamp}.${extension}`;
    
    // 3. Subir a Supabase Storage
    const publicUrl = await uploadToSupabaseStorage(buffer, filePath, mimeType);
    
    // 4. Procesar con Gemini
    const model = genAI.getGenerativeModel({ model: "gemini-2.0-flash-exp" });
    
    const result = await model.generateContent([
      {
        inlineData: {
          data: buffer.toString('base64'),
          mimeType: mimeType
        }
      },
      "Describe esta imagen de manera concisa y útil para un asistente de atención al cliente. Si parece relacionada con un servicio estético, menciona detalles relevantes."
    ]);
    
    const description = result.response.text();
    
    return {
      processedText: `[Imagen enviada]: ${description}`,
      mediaUrl: publicUrl,
      mediaType: 'image',
      mimeType: mimeType
    };
    
  } catch (error) {
    console.error('Error procesando imagen:', error);
    return {
      processedText: '[Imagen recibida - Error en procesamiento]',
      mediaUrl: null,
      mediaType: 'image',
      mimeType: 'unknown'
    };
  }
}

/**
 * Maneja tipos de media no procesables (video, documentos, etc.)
 */
async function handleUnsupportedMedia(mediaUrl, mediaType, organizationId, chatIdentityId) {
  try {
    console.log(`📎 Guardando ${mediaType} sin procesar...`);
    
    // 1. Descargar archivo
    const { buffer, mimeType } = await downloadFile(mediaUrl);
    
    // 2. Generar nombre de archivo
    const timestamp = Date.now();
    const extension = getFileExtension(mimeType, mediaType);
    const filePath = `${organizationId}/${chatIdentityId}/${timestamp}.${extension}`;
    
    // 3. Subir a Supabase Storage
    const publicUrl = await uploadToSupabaseStorage(buffer, filePath, mimeType);
    
    // 4. Mensaje de fallback
    const fallbackMessages = {
      'video': 'He recibido tu video, pero no puedo procesarlo automáticamente. ¿Te gustaría hablar con un asesor para revisarlo juntos?',
      'file': 'He recibido tu documento, pero no puedo procesarlo automáticamente. ¿Te gustaría hablar con un asesor para revisarlo juntos?',
      'document': 'He recibido tu documento, pero no puedo procesarlo automáticamente. ¿Te gustaría hablar con un asesor para revisarlo juntos?',
      'default': 'He recibido tu archivo, pero no puedo procesarlo automáticamente. ¿Te gustaría hablar con un asesor?'
    };
    
    return {
      processedText: fallbackMessages[mediaType] || fallbackMessages.default,
      mediaUrl: publicUrl,
      mediaType: mediaType,
      mimeType: mimeType
    };
    
  } catch (error) {
    console.error(`Error guardando ${mediaType}:`, error);
    return {
      processedText: 'He recibido tu archivo, pero hubo un problema guardándolo. ¿Podrías enviarlo de nuevo o hablar con un asesor?',
      mediaUrl: null,
      mediaType: mediaType,
      mimeType: 'unknown'
    };
  }
}

/**
 * Maneja location (coordenadas GPS)
 */
async function handleLocation(locationData, organizationId, chatIdentityId) {
  try {
    console.log('📍 Procesando ubicación...');
    
    const latitude = locationData.latitude;
    const longitude = locationData.longitude;
    
    if (!latitude || !longitude) {
      throw new Error('Coordenadas de ubicación incompletas');
    }
    
    // No hay archivo que guardar, solo las coordenadas
    const locationText = `Ubicación compartida: Latitud ${latitude}, Longitud ${longitude}`;
    
    return {
      processedText: `[Ubicación recibida]: ${locationText}. ¡Gracias por compartir tu ubicación!`,
      mediaUrl: null, // No hay archivo
      mediaType: 'location',
      mimeType: 'application/json'
    };
    
  } catch (error) {
    console.error('Error procesando ubicación:', error);
    return {
      processedText: '[Ubicación recibida - Error en procesamiento]',
      mediaUrl: null,
      mediaType: 'location',
      mimeType: null
    };
  }
}

/**
 * Maneja contacto compartido
 */
async function handleContact(contactData, organizationId, chatIdentityId) {
  try {
    console.log('👤 Procesando contacto compartido...');
    
    const contacts = contactData.contacts || [];
    if (contacts.length === 0) {
      throw new Error('No hay información de contacto');
    }
    
    const contact = contacts[0]; // Tomar el primer contacto
    const name = contact.name?.formatted_name || 'Contacto sin nombre';
    const phone = contact.phones?.[0]?.phone || 'Sin teléfono';
    
    const contactText = `Contacto: ${name} - ${phone}`;
    
    return {
      processedText: `[Contacto compartido]: ${contactText}. He recibido la información de contacto.`,
      mediaUrl: null, // No hay archivo
      mediaType: 'contact',
      mimeType: 'application/json'
    };
    
  } catch (error) {
    console.error('Error procesando contacto:', error);
    return {
      processedText: '[Contacto recibido - Error en procesamiento]',
      mediaUrl: null,
      mediaType: 'contact',
      mimeType: null
    };
  }
}

/**
 * Obtiene extensión de archivo basada en MIME type
 */
function getFileExtension(mimeType, mediaType) {
  const extensions = {
    'video/mp4': 'mp4',
    'video/quicktime': 'mov',
    'application/pdf': 'pdf',
    'application/msword': 'doc',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
    'image/jpeg': 'jpg',
    'image/png': 'png',
    'audio/ogg': 'ogg',
    'audio/mpeg': 'mp3'
  };
  
  return extensions[mimeType] || mediaType || 'file';
}

/**
 * Función principal para procesar cualquier tipo de media
 */
async function processMedia(payload, organizationId, chatIdentityId) {
  const mediaType = payload?.type;
  
  if (!mediaType) {
    return null;
  }
  
  console.log(`🔄 Procesando media tipo: ${mediaType}`);
  
  switch (mediaType) {
    case 'audio':
      if (!payload?.url) return null;
      return await processAudio(payload.url, organizationId, chatIdentityId);
    
    case 'image':
      if (!payload?.url) return null;
      return await processImage(payload.url, organizationId, chatIdentityId);
    
    case 'video':
      if (!payload?.url) return null;
      return await handleUnsupportedMedia(payload.url, 'video', organizationId, chatIdentityId);
      
    case 'file': // Documentos en Gupshup se llaman 'file'
      if (!payload?.url) return null;
      return await handleUnsupportedMedia(payload.url, 'file', organizationId, chatIdentityId);
    
    case 'location':
      return await handleLocation(payload, organizationId, chatIdentityId);
    
    case 'contact':
      return await handleContact(payload, organizationId, chatIdentityId);
    
    default:
      console.log(`❓ Tipo de media desconocido: ${mediaType}`);
      return null;
  }
}

module.exports = {
  processMedia
}; 