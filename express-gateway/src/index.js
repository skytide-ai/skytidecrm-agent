require('dotenv').config();
const express = require('express');
const cors = require('cors');
const helmet = require('helmet');
const morgan = require('morgan');
const axios = require('axios');

const webhooksRouter = require('./routes/webhooks'); // Importar el enrutador
const internalRouter = require('./routes/internal'); // Importar el enrutador interno

const app = express();
const PORT = process.env.GATEWAY_PORT || 8080;

// Middlewares
app.use(cors()); // Habilita CORS para todas las rutas
app.use(helmet()); // Añade varias cabeceras de seguridad
app.use(express.json()); // Parsea bodies de requests como JSON
app.use(morgan('dev')); // Logger de peticiones HTTP en modo desarrollo

// Rutas
app.use('/webhooks', webhooksRouter); // Usar el enrutador de webhooks
app.use('/internal', internalRouter); // Usar el enrutador interno

// Ruta de chat para pruebas directas - proxy al servicio Python
app.post('/chat', async (req, res) => {
  try {
    console.log('🔄 Proxying chat request to Python service...');
    const response = await axios.post('http://python-service:8000/chat', req.body, {
      headers: {
        'Content-Type': 'application/json'
      },
      timeout: 60000 // 60 segundos
    });
    res.json(response.data);
  } catch (error) {
    console.error('❌ Error proxying to Python service:', error.message);
    res.status(500).json({ 
      error: 'Error connecting to chat service',
      details: error.message 
    });
  }
});

// Ruta de prueba para verificar que el servidor está funcionando
app.get('/', (req, res) => {
  res.send('API Gateway is running...');
});

// Iniciar el servidor
app.listen(PORT, () => {
  console.log(`API Gateway listening on port ${PORT}`);
}); 