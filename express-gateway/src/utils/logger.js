const pino = require('pino');

// Configuración para desarrollo (logs bonitos)
const devOptions = {
  transport: {
    target: 'pino-pretty',
    options: {
      colorize: true,
      translateTime: 'HH:MM:ss',
      ignore: 'pid,hostname',
      messageFormat: '{msg} [org:{organization_id}] [chat:{chat_identity_id}]'
    }
  }
};

// Configuración para producción (JSON estructurado)
const prodOptions = {
  level: process.env.LOG_LEVEL || 'info',
  formatters: {
    level: (label) => {
      return { level: label.toUpperCase() };
    }
  },
  timestamp: pino.stdTimeFunctions.isoTime,
};

// Crear logger según ambiente
const logger = pino(
  process.env.NODE_ENV === 'production' ? prodOptions : devOptions
);

// Helper functions para logging consistente
const logWebhook = (message, data = {}) => {
  logger.info({
    type: 'webhook',
    ...data
  }, `🔍 ${message}`);
};

const logError = (message, error, data = {}) => {
  logger.error({
    type: 'error',
    error: error?.message || error,
    stack: error?.stack,
    ...data
  }, `❌ ${message}`);
};

const logMedia = (message, data = {}) => {
  logger.info({
    type: 'media',
    ...data
  }, `📎 ${message}`);
};

const logResponse = (message, data = {}) => {
  logger.info({
    type: 'response',
    ...data
  }, `✅ ${message}`);
};

module.exports = {
  logger,
  logWebhook,
  logError,
  logMedia,
  logResponse
};