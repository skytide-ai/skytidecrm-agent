# Configuración Docker para SkytideCRM Agent

## Variables de Entorno Requeridas

Antes de ejecutar el docker-compose, necesitas configurar las siguientes variables de entorno. Crea un archivo `.env` en la raíz del proyecto con:

```bash
# Configuración de Supabase
SUPABASE_URL=https://tu-proyecto.supabase.co
SUPABASE_ANON_KEY=tu-anon-key
SUPABASE_SERVICE_ROLE_KEY=tu-service-role-key

# Configuración de Redis (opcional pero recomendado en prod)
# Formato: redis://[:password]@host:port/0
REDIS_URL=redis://localhost:6379/0

# Configuración de OpenAI
OPENAI_API_KEY=tu-openai-api-key
# Modelo de chat (opcional). Por defecto: gpt-4o
OPENAI_CHAT_MODEL=gpt-4o

# Configuración de Gemini AI (para procesamiento de medios)
GEMINI_API_KEY=tu-gemini-api-key

# Configuración de Webhooks (opcional)
WEBHOOK_SECRET=tu-webhook-secret
```

## Arquitectura de Servicios

### 1. **python-service** (Puerto 8000)
- Agente principal con LangGraph
- Maneja Knowledge, Appointment, Confirmation, Cancellation, Reschedule y Escalation
- Integración con Supabase (historial y resúmenes) y Redis como checkpointer (si `REDIS_URL` está definido)
- Healthcheck en `http://localhost:8000/`

### 2. **express-gateway** (Puerto 8080)
- API Gateway para webhooks y rutas internas
- Maneja autenticación y procesamiento de medios
- Healthcheck en `http://localhost:8080/`

## Comandos de Docker

### Iniciar los servicios
```bash
docker-compose up -d
```

### Ver logs en tiempo real
```bash
# Todos los servicios
docker-compose logs -f

# Solo el agente Python
docker-compose logs -f python-service

# Solo el gateway
docker-compose logs -f express-gateway
```

### Verificar estado de los servicios
```bash
docker-compose ps
```

### Reiniciar un servicio específico
```bash
docker-compose restart python-service
docker-compose restart express-gateway
```

### Detener todos los servicios
```bash
docker-compose down
```

### Reconstruir imágenes (después de cambios en código)
```bash
docker-compose up --build -d
```

## Endpoints Disponibles

### Python Service (puerto 8000)
- `GET /` - Health check
- `POST /invoke` - Endpoint principal del agente

### Express Gateway (puerto 8080)
- `GET /` - Health check
- `POST /webhooks/*` - Endpoints de webhooks
- `POST /internal/*` - Endpoints internos

## Resolución de Problemas

### 1. **Error de conexión entre servicios**
```bash
# Verificar que ambos servicios estén en la misma red
docker network ls
docker network inspect skytidecrm-network
```

### 2. **Variables de entorno no cargadas**
```bash
# Verificar que el archivo .env existe y tiene las variables correctas
cat .env

# Recrear los contenedores
docker-compose down
docker-compose up -d
```

### 3. **Problemas de puertos ocupados**
```bash
# Verificar qué proceso usa el puerto
netstat -tulpn | grep :8000
netstat -tulpn | grep :8080

# Cambiar puertos en docker-compose.yml si es necesario
```

### 4. **Logs de errores**
```bash
# Ver logs detallados
docker-compose logs --tail=100 python-service
docker-compose logs --tail=100 express-gateway
```

## Red de Docker

Los servicios se comunican internamente a través de la red `skytidecrm-network`:
- `python-service` → `express-gateway:8080`
- `express-gateway` → `python-service:8000`

## Notas de Desarrollo

1. **Hot Reload**: Para desarrollo, puedes montar volúmenes para hot reload
2. **Debug**: Los logs están configurados para mostrar información detallada
3. **Health Checks**: Ambos servicios tienen health checks configurados
4. **Persistencia**: Se incluye un volumen para datos persistentes si es necesario

## Configuración Específica del Agente

Este docker-compose está configurado específicamente para el **SkytideCRM Agent** con:
- Integración completa con Supabase
- Memoria conversacional basada en Supabase (`chat_messages.processed_text` y `thread_summaries`)  
- Durabilidad del grafo con Redis como checkpointer (si `REDIS_URL` está definido)
- Arquitectura de agentes con LangGraph
- Gateway para webhooks y API
- Red interna optimizada para comunicación entre servicios

### Detalles de Memoria (Supabase + Redis)
- Historial corto: el servicio Python lee los últimos N mensajes desde `chat_messages` priorizando `processed_text`.
- Contexto largo: el resumen del hilo se guarda/lee desde `thread_summaries`.
- Estado del grafo: se persiste en Redis mediante el checkpointer de LangGraph (si `REDIS_URL` está presente). En ausencia de Redis, usa memoria en proceso.

### Ejemplos de `REDIS_URL`
- Local: `REDIS_URL=redis://localhost:6379/0`
- Docker (servicio llamado `redis`): `REDIS_URL=redis://redis:6379/0`
- Producción (Aiven/Upstash/ElastiCache): `REDIS_URL=redis://:PASSWORD@HOST:PORT/0`