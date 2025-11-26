# SkytideCRM Agent 🤖

Sistema de agente inteligente para automatización de atención al cliente y gestión de citas vía WhatsApp, desarrollado con LangGraph, FastAPI y Express.js.

## 📋 Tabla de Contenidos

- [Características](#características)
- [Arquitectura](#arquitectura)
- [Requisitos](#requisitos)
- [Instalación](#instalación)
- [Configuración](#configuración)
- [Uso](#uso)
- [API Reference](#api-reference)
- [Estructura del Proyecto](#estructura-del-proyecto)
- [Flujo de Datos](#flujo-de-datos)
- [Seguridad](#seguridad)
- [Observabilidad y Monitoring](#observabilidad-y-monitoring)
- [Troubleshooting](#troubleshooting)

## ✨ Características

- **Agente Conversacional Inteligente**: Procesamiento de lenguaje natural con OpenAI GPT-4
- **Gestión de Citas Automatizada**: Agendamiento, cancelación, confirmación y reagendamiento
- **Procesamiento Multimedia**: Transcripción de audio y descripción de imágenes con Gemini AI
- **Multi-tenancy**: Soporte para múltiples organizaciones con aislamiento de datos
- **Memoria Persistente**: Contexto de conversación mantenido con Redis/MemorySaver
- **Escalamiento Humano**: Sistema de notificaciones para intervención humana cuando es necesaria
- **Integración WhatsApp**: Comunicación bidireccional vía Gupshup API

## 🏗️ Arquitectura

### Componentes Principales

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│    WhatsApp     │────▶│  Express Gateway │────▶│  Python Service │
│    (Gupshup)    │◀────│   (Port 8080)    │◀────│   (Port 8000)   │
└─────────────────┘     └──────────────────┘     └─────────────────┘
                               │                         │
                               ▼                         ▼
                        ┌──────────────┐         ┌──────────────┐
                        │   Gemini AI  │         │   OpenAI     │
                        │ (Media Proc) │         │   GPT-4o     │
                        └──────────────┘         └──────────────┘
                               │                         │
                               └────────┬────────────────┘
                                        ▼
                               ┌──────────────────┐
                               │     Supabase     │
                               │   (PostgreSQL)   │
                               └──────────────────┘
```

### Microservicios

1. **Express Gateway** (`express-gateway/`)
   - Maneja webhooks de WhatsApp/Gupshup
   - Procesa contenido multimedia (audio, imágenes)
   - Gestiona autenticación y multi-tenancy
   - Implementa debounce de mensajes (10s)

2. **Python Service** (`python-service/`)
   - Motor de IA con LangGraph
   - Sistema de nodos especializados
   - Gestión de estado y memoria
   - Herramientas de integración con CRM

## 📦 Requisitos

### Software
- Docker & Docker Compose
- Node.js 20+ (desarrollo local)
- Python 3.11+ (desarrollo local)
- PostgreSQL (via Supabase)
- Redis (opcional, para producción)

### Servicios Externos
- Cuenta Supabase con base de datos configurada
- API Key de OpenAI
- API Key de Gemini AI
- Cuenta Gupshup con app WhatsApp configurada

## 🚀 Instalación

### 1. Clonar el Repositorio

```bash
git clone https://github.com/tu-org/skytidecrm-agent.git
cd skytidecrm-agent
```

### 2. Configurar Variables de Entorno

Crear archivo `.env` en la raíz del proyecto:

```env
# Supabase
SUPABASE_URL=https://tu-proyecto.supabase.co
SUPABASE_SERVICE_ROLE_KEY=tu-service-role-key
SUPABASE_ANON_KEY=tu-anon-key

# OpenAI
OPENAI_API_KEY=sk-...
OPENAI_CHAT_MODEL=gpt-4o  # Opcional

# Gemini AI (para procesamiento multimedia)
GEMINI_API_KEY=AIza...

# Redis (opcional, para checkpointing en producción)
REDIS_URL=redis://localhost:6379/0

# Gupshup (notificaciones internas)
GUPSHUP_NOTIF_API_KEY=tu-api-key
GUPSHUP_NOTIF_SOURCE=573001234567  # Número emisor
GUPSHUP_ESCALATION_TEMPLATE_ID=template-uuid
GUPSHUP_NOTIF_APP_NAME=tu-app-name

# Gateway
GATEWAY_PORT=8080
PYTHON_SERVICE_URL=http://python-service:8000
AI_AGENT_ENABLED=true  # Desactivar agente IA temporalmente (false = solo procesa mensajes, no responde)
```

### 3. Configurar Base de Datos

Ejecutar las migraciones necesarias en Supabase:

```sql
-- Tablas principales requeridas:
-- platform_connections: Configuración WhatsApp por organización
-- chat_identities: Identidades de chat por usuario
-- chat_messages: Historial de mensajes
-- contacts: Información de contactos
-- appointments: Citas agendadas
-- services: Catálogo de servicios
-- members: Personal que atiende
-- internal_notifications_config: Config de notificaciones
```

### 4. Iniciar con Docker Compose

```bash
# Desarrollo (con logs visibles)
docker-compose up

# Producción (background)
docker-compose up -d

# Ver logs
docker-compose logs -f python-service
docker-compose logs -f express-gateway
```

## ⚙️ Configuración

### Configuración de Webhook en Gupshup

1. En tu app de Gupshup, configurar webhook URL:
   ```
   https://tu-dominio.com/webhooks/gupshup
   ```

2. Registrar la conexión en `platform_connections`:
   ```sql
   INSERT INTO platform_connections (
     organization_id,
     platform,
     gupshup_app_name,
     gupshup_api_key,
     whatsapp_business_number,
     is_active
   ) VALUES (
     'org-uuid',
     'whatsapp',
     'tu-app-name',
     'tu-api-key',
     '573001234567',
     true
   );
   ```

### Configuración de Servicios

Los servicios disponibles para agendamiento deben estar en la tabla `services`:
```sql
INSERT INTO services (
  organization_id,
  name,
  description,
  duration_minutes,
  is_bookable,
  requires_assessment
) VALUES (
  'org-uuid',
  'Depilación Láser',
  'Tratamiento de depilación con tecnología láser',
  60,
  true,
  false
);
```

## 📡 API Reference

### Express Gateway

#### `POST /webhooks/gupshup`
Recibe mensajes de WhatsApp vía Gupshup.

**Headers Requeridos**: Ninguno (autenticación via app name)

**Body Example**:
```json
{
  "app": "nombre-app",
  "type": "message",
  "payload": {
    "id": "msg-id",
    "source": "573001234567",
    "type": "text",
    "payload": {
      "text": "Hola, quiero agendar una cita"
    },
    "sender": {
      "phone": "573001234567",
      "country_code": "57",
      "dial_code": "3001234567"
    }
  }
}
```

#### `POST /internal/notify/escalation`
Notifica escalamiento a asesor humano.

**Body**:
```json
{
  "organization_id": "org-uuid",
  "chat_identity_id": "chat-uuid",
  "phone_number": "3001234567",
  "country_code": "+57",
  "reason": "Cliente solicita hablar con un asesor"
}
```

### Python Service

#### `POST /invoke`
Procesa mensaje y genera respuesta del agente.

**Body**:
```json
{
  "organizationId": "org-uuid",
  "chatIdentityId": "chat-uuid",
  "contactId": "contact-uuid",
  "phone": "573001234567",
  "phoneNumber": "3001234567",
  "countryCode": "+57",
  "message": "Quiero agendar una cita",
  "recentMessages": [
    {"role": "user", "content": "Hola"},
    {"role": "assistant", "content": "¡Hola! ¿En qué puedo ayudarte?"}
  ]
}
```

**Response**:
```json
{
  "response": "¡Por supuesto! ¿Qué servicio te gustaría agendar?"
}
```

## 📁 Estructura del Proyecto

```
skytidecrm-agent/
├── docker-compose.yml           # Configuración Docker
├── .env                         # Variables de entorno (NO COMMITEAR)
├── express-gateway/             # Servicio Gateway Node.js
│   ├── Dockerfile
│   ├── package.json
│   └── src/
│       ├── index.js            # Servidor Express principal
│       ├── middlewares/
│       │   └── auth.js         # Resolución de organización
│       ├── routes/
│       │   ├── webhooks.js    # Manejo de webhooks WhatsApp
│       │   └── internal.js    # Endpoints internos
│       └── utils/
│           ├── mediaProcessor.js  # Procesamiento multimedia
│           └── gupshupApi.js     # Cliente API Gupshup
└── python-service/              # Servicio Python IA
    ├── Dockerfile
    ├── requirements.txt
    └── app/
        ├── main.py             # FastAPI + LangGraph
        ├── state.py            # Definición de estado global
        ├── tools.py            # Herramientas del agente
        ├── memory.py           # Gestión de memoria
        └── db.py               # Cliente Supabase
```

## 🔄 Flujo de Datos

### Flujo de Mensaje Entrante

1. **WhatsApp → Gupshup**: Usuario envía mensaje
2. **Gupshup → Express Gateway**: Webhook POST a `/webhooks/gupshup`
3. **Gateway Procesa**:
   - Resuelve organización via `platform_connections`
   - Crea/actualiza `chat_identity`
   - Procesa media si existe (audio→texto, imagen→descripción)
   - Guarda en `chat_messages`
4. **Gateway → Python Service**: Invoca `/invoke` con mensaje
5. **Python Service**:
   - Carga estado desde Redis/MemorySaver
   - Supervisor decide nodo apropiado
   - Nodo ejecuta lógica y herramientas
   - Genera respuesta
6. **Python → Gateway**: Retorna respuesta
7. **Gateway → Gupshup**: Envía mensaje de vuelta
8. **Gupshup → WhatsApp**: Usuario recibe respuesta

### Nodos del Agente

- **`supervisor`**: Enrutador principal que decide qué nodo manejar
- **`knowledge`**: Responde preguntas sobre servicios e información general
- **`appointment`**: Gestiona el flujo de agendamiento de citas
- **`cancellation`**: Maneja cancelación de citas existentes
- **`confirmation`**: Confirma citas pendientes
- **`reschedule`**: Reagenda citas existentes
- **`escalation`**: Escala a asesor humano cuando es necesario

### Herramientas Disponibles

- `knowledge_search`: Búsqueda semántica en base de conocimiento
- `check_availability`: Consulta disponibilidad de horarios
- `book_appointment`: Crea nueva cita
- `cancel_appointment`: Cancela cita existente
- `confirm_appointment`: Confirma cita pendiente
- `reschedule_appointment`: Cambia fecha/hora de cita
- `escalate_to_human`: Notifica a asesor humano
- `resolve_contact_on_booking`: Crea/obtiene contacto CRM

## 🔒 Seguridad

### Consideraciones Importantes

1. **NUNCA commitear `.env`** - Contiene credenciales sensibles
2. **Rotar credenciales regularmente**
3. **Usar Service Role Key solo en backend**
4. **Implementar rate limiting en producción**
5. **Validar todos los inputs del usuario**
6. **Configurar CORS apropiadamente**
7. **Usar HTTPS en producción**

### Recomendaciones

- Implementar autenticación en webhooks (signature verification)
- Usar secrets manager (HashiCorp Vault, AWS Secrets Manager)
- Configurar políticas RLS en Supabase
- Implementar logging sin PII
- Configurar WAF para protección adicional

## 📊 Observabilidad y Monitoring

### Langfuse Cloud (Trazas de IA)

El proyecto incluye integración con **Langfuse Cloud** para monitorear el comportamiento del agente IA:

#### Configuración (5 minutos)

1. **Crear cuenta gratuita** en [cloud.langfuse.com](https://cloud.langfuse.com)
2. **Crear proyecto** → Obtener API Keys
3. **Agregar a `.env`**:
```env
LANGFUSE_HOST=https://cloud.langfuse.com
LANGFUSE_PUBLIC_KEY=pk_lf_xxxxx
LANGFUSE_SECRET_KEY=sk_lf_xxxxx
```
4. **Reiniciar**: `docker-compose restart python-service`

#### ¿Qué puedes ver?

- **Trazas completas**: Cada decisión del agente paso a paso
- **Métricas de uso**: Tokens consumidos, costos por conversación
- **Performance**: Latencia por nodo, cuellos de botella
- **Debug**: Ver exactamente qué prompt se usó y qué respondió GPT

Ejemplo de traza:
```
Trace ID: abc-123 | Usuario: "quiero agendar"
├── 🧠 Supervisor (120ms, 234 tokens, $0.002)
│   └── Decision: route to 'appointment'
├── 📅 Appointment Node (450ms, 123 tokens, $0.001)
│   ├── Tool: knowledge_search
│   └── Response: "¿Qué servicio deseas?"
└── Total: 570ms, $0.003
```

**Límites**: 50,000 trazas gratis/mes (suficiente para ~5,000 conversaciones)

### Logs del Sistema

#### Visualización Rápida de Logs

El proyecto incluye un script helper `logs.sh` para ver logs organizados:

```bash
# Hacer el script ejecutable (primera vez)
chmod +x logs.sh

# Ver logs del gateway
./logs.sh gateway

# Ver logs del agente Python
./logs.sh python

# Ver solo errores
./logs.sh errors

# Ver webhooks recientes
./logs.sh webhooks

# Filtrar por organización específica
./logs.sh gateway "org-123"
```

#### Comandos Docker Directos

```bash
# Logs en tiempo real (todos los servicios)
docker-compose logs -f

# Logs del Gateway
docker-compose logs -f express-gateway

# Logs del Python Service
docker-compose logs -f python-service

# Últimos 100 logs con timestamp
docker-compose logs -t --tail=100

# Logs de las últimas 2 horas
docker-compose logs --since 2h

# Buscar errores
docker-compose logs | grep -E "ERROR|error|Error"
```

#### Estructura de Logs

**Express Gateway** usa Pino con logs estructurados:
```
🔍 WEBHOOK recibido [org:org-123] [chat:chat-456]
📎 Procesando audio 10MB [org:org-123]
✅ Respuesta enviada (770ms) [org:org-123]
❌ Error: Timeout al contactar Python service
```

**Python Service** incluye información del flujo:
```
🧠 NODO: Supervisor - Decisión: 'appointment'
📅 NODO: Appointment - Servicio: Depilación Láser
🧰 Herramienta: check_availability(2024-01-15)
✅ Cita agendada: appointment-uuid
```

### Métricas Clave a Monitorear

#### En Langfuse Cloud
- **Success Rate**: > 95% esperado
- **Latencia P95**: < 2 segundos
- **Tokens por conversación**: < 1000 promedio
- **Costo diario**: Monitorear para optimización

#### En Logs
- **Error rate**: < 1% 
- **Timeouts**: Investigar si > 5/hora
- **Escalamientos**: Revisar si > 10% de conversaciones
- **Media processing**: Verificar si falla frecuentemente

### Alertas Recomendadas

Aunque no hay sistema de alertas automático, revisa regularmente:

```bash
# Crear cron job para revisar errores cada hora
0 * * * * docker-compose logs --since 1h | grep -E "ERROR|CRITICAL" | wc -l

# Si hay más de 10 errores en la última hora, investigar
```

## 🐛 Troubleshooting

### El agente no responde

1. Verificar que `AI_AGENT_ENABLED=true` en variables de entorno del Gateway
2. Verificar logs: `docker-compose logs -f python-service`
3. Confirmar que Redis/MemorySaver está funcionando
4. Verificar que las API keys son válidas
5. Revisar que `chat_identities.bot_enabled = true` en la BD

### Desactivar agente IA temporalmente (sin perder mensajes)

Para desactivar el agente IA pero mantener el procesamiento de mensajes (transcripción de audio, guardado en BD, etc.):

1. Cambiar variable de entorno en el Gateway:
   ```
   AI_AGENT_ENABLED=false
   ```
2. Reiniciar el servicio Gateway
3. Los mensajes seguirán siendo:
   - Recibidos y deduplicados
   - Procesados (audio → texto, imágenes → descripción)
   - Guardados en `chat_messages`
4. El agente IA **no responderá** hasta que se reactive con `AI_AGENT_ENABLED=true`

### Mensajes duplicados

- El sistema tiene deduplicación de 5 minutos
- Verificar que el webhook no está siendo llamado múltiples veces
- Revisar configuración de debounce (default 10s)

### Error de organización no encontrada

1. Verificar que `gupshup_app_name` coincide en `platform_connections`
2. Confirmar que `is_active = true`
3. Verificar que el `platform = 'whatsapp'`

### Media no se procesa

1. Verificar API key de Gemini
2. Revisar límites de tamaño (máx 10MB por defecto)
3. Confirmar que el tipo de media es soportado (audio, imagen)

## 📝 Licencia

Propietario - Todos los derechos reservados

## 🤝 Contribución

Para contribuir al proyecto:
1. Fork el repositorio
2. Crea una rama feature (`git checkout -b feature/AmazingFeature`)
3. Commit tus cambios (`git commit -m 'Add AmazingFeature'`)
4. Push a la rama (`git push origin feature/AmazingFeature`)
5. Abre un Pull Request

## 📞 Soporte

Para soporte y consultas, contactar al equipo de desarrollo.

---

Desarrollado con ❤️ por Skytide Agency