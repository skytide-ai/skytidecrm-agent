require('dotenv').config();
const express = require('express');
const cors = require('cors');
const helmet = require('helmet');
const morgan = require('morgan');
const pino = require('pino');
const pinoHttp = require('pino-http');
const { createWriteStream } = require('node:stream');
const axios = require('axios');

const webhooksRouter = require('./routes/webhooks'); // Importar el enrutador
const internalRouter = require('./routes/internal'); // Importar el enrutador interno

const app = express();
const PORT = process.env.GATEWAY_PORT || 8080;

// Middlewares
app.use(cors());
app.use(helmet());
app.use(express.json());

// Logger estructurado (JSON) con campos útiles para filtrado
const logger = pino({ level: process.env.LOG_LEVEL || 'info' });
app.use(pinoHttp({
  logger,
  customProps: (req, res) => ({
    organization_id: req.headers['x-organization-id'] || null,
    request_id: req.headers['x-request-id'] || null,
    route: req.originalUrl,
  }),
}));

// Morgan dev en consola si se desea (opcional)
if (process.env.ENABLE_MORGAN === 'true') {
  app.use(morgan('dev'));
}

// Rutas
app.use('/webhooks', webhooksRouter); // Usar el enrutador de webhooks
app.use('/internal', internalRouter); // Usar el enrutador interno


// Ruta de prueba para verificar que el servidor está funcionando
app.get('/', (req, res) => {
  res.send('API Gateway is running...');
});

// Iniciar el servidor
app.listen(PORT, () => {
  console.log(`API Gateway listening on port ${PORT}`);
}); 