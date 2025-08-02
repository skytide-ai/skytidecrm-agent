const axios = require('axios');

/**
 * Envía un mensaje de texto a través de la API de Gupshup
 * 
 * @param {string} apiKey - API key de Gupshup para la organización
 * @param {string} sourceNumber - Número de WhatsApp Business (fuente)
 * @param {string} destinationNumber - Número de destinatario
 * @param {string} message - Mensaje de texto a enviar
 * @returns {Promise<Object>} Respuesta de la API de Gupshup
 */
async function sendTextMessage(apiKey, sourceNumber, destinationNumber, message) {
  try {
    console.log(`📤 Enviando mensaje via Gupshup:`);
    console.log(`   De: ${sourceNumber}`);
    console.log(`   Para: ${destinationNumber}`);
    console.log(`   Mensaje: ${message.substring(0, 100)}${message.length > 100 ? '...' : ''}`);

    // Crear payload en formato form-urlencoded según documentación de Gupshup
    const payload = new URLSearchParams({
      channel: 'whatsapp',
      source: sourceNumber,
      destination: destinationNumber,
      message: message
    });

    const response = await axios.post(
      'https://api.gupshup.io/wa/api/v1/msg',
      payload,
      {
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
          'apikey': apiKey,
          'Cache-Control': 'no-cache'
        },
        timeout: 10000 // 10 segundos de timeout
      }
    );

    console.log('✅ Mensaje enviado exitosamente via Gupshup:');
    console.log(`   MessageId: ${response.data.messageId}`);
    console.log(`   Status: ${response.data.status}`);

    return {
      success: true,
      messageId: response.data.messageId,
      status: response.data.status,
      data: response.data
    };

  } catch (error) {
    console.error('❌ Error enviando mensaje via Gupshup:', error.message);
    
    if (error.response) {
      // El servidor de Gupshup respondió con un error
      console.error('Gupshup API Error Response:', error.response.data);
      console.error('Status Code:', error.response.status);
      
      return {
        success: false,
        error: error.response.data,
        statusCode: error.response.status
      };
    } else if (error.request) {
      // La petición fue hecha pero no se recibió respuesta
      console.error('No response received from Gupshup API');
      return {
        success: false,
        error: 'No response from Gupshup API',
        statusCode: 503
      };
    } else {
      // Error configurando la petición
      console.error('Error setting up request:', error.message);
      return {
        success: false,
        error: 'Request configuration error',
        statusCode: 500
      };
    }
  }
}

/**
 * Envía un mensaje con quick replies a través de la API de Gupshup
 * 
 * @param {string} apiKey - API key de Gupshup
 * @param {string} sourceNumber - Número de WhatsApp Business
 * @param {string} destinationNumber - Número de destinatario  
 * @param {string} text - Texto del mensaje
 * @param {Array} options - Array de opciones [{title: 'Opción 1', postbackText: 'option1'}, ...]
 * @returns {Promise<Object>} Respuesta de la API de Gupshup
 */
async function sendQuickReplyMessage(apiKey, sourceNumber, destinationNumber, text, options) {
  try {
    console.log(`📤 Enviando mensaje con quick replies via Gupshup:`);
    console.log(`   Para: ${destinationNumber}`);
    console.log(`   Opciones: ${options.length}`);

    const payload = {
      source: sourceNumber,
      destination: destinationNumber,
      message: {
        type: 'quick_reply',
        content: {
          type: 'text',
          text: text
        },
        options: options.map(option => ({
          type: 'text',
          title: option.title,
          postbackText: option.postbackText || option.title
        }))
      }
    };

    const response = await axios.post(
      'https://api.gupshup.io/wa/api/v1/msg',
      payload,
      {
        headers: {
          'Content-Type': 'application/json',
          'apikey': apiKey
        },
        timeout: 10000
      }
    );

    console.log('✅ Mensaje con quick replies enviado exitosamente');
    return {
      success: true,
      messageId: response.data.messageId,
      status: response.data.status,
      data: response.data
    };

  } catch (error) {
    console.error('❌ Error enviando quick reply via Gupshup:', error.message);
    return {
      success: false,
      error: error.response?.data || error.message,
      statusCode: error.response?.status || 500
    };
  }
}

module.exports = {
  sendTextMessage,
  sendQuickReplyMessage
};