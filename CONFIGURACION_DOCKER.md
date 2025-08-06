# Configuración Docker para SkytideCRM Agent

## Variables de Entorno Requeridas

Antes de ejecutar el docker-compose, necesitas configurar las siguientes variables de entorno. Crea un archivo `.env` en la raíz del proyecto con:

```bash
# Configuración de Supabase
SUPABASE_URL=https://tu-proyecto.supabase.co
SUPABASE_ANON_KEY=tu-anon-key
SUPABASE_SERVICE_ROLE_KEY=tu-service-role-key

# Configuración de Zep Cloud
ZEP_API_KEY=tu-zep-api-key

# Configuración de OpenAI
OPENAI_API_KEY=tu-openai-api-key

# Configuración de Gemini AI (para procesamiento de medios)
GEMINI_API_KEY=tu-gemini-api-key

# Configuración de Webhooks (opcional)
WEBHOOK_SECRET=tu-webhook-secret
```

## Arquitectura de Servicios

### 1. **python-service** (Puerto 8000)
- Agente principal con LangGraph
- Maneja Knowledge, Appointment y Escalation agents
- Integración con Supabase y Zep Cloud
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
- Memoria persistente con Zep Cloud  
- Arquitectura de agentes con LangGraph
- Gateway para webhooks y API
- Red interna optimizada para comunicación entre servicios