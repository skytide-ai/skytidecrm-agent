# Guía de Despliegue en Easypanel

## Configuración en Easypanel

### 1. Configuración de la Fuente

En la pantalla de configuración de Easypanel, selecciona:

- **Fuente**: `Git`
- **URL del repositorio**: `https://github.com/skytide-ai/skytidecrm-agent.git`
- **Rama**: `main` (o `PydanticAgent` si quieres usar la rama actual)
- **Ruta de compilación**: `/` (dejar como está)
- **Archivo Docker Compose**: `docker-compose.yml` (dejar como está)

### 2. Variables de Entorno

Después de guardar la configuración inicial, necesitas configurar las siguientes variables de entorno en Easypanel:

#### Variables Requeridas:

```env
# Supabase
SUPABASE_URL=https://tu-proyecto.supabase.co
SUPABASE_SERVICE_ROLE_KEY=tu-service-role-key
SUPABASE_ANON_KEY=tu-anon-key
SUPABASE_DB_PASSWORD=tu-db-password

# OpenAI
OPENAI_API_KEY=sk-proj-...
OPENAI_CHAT_MODEL=gpt-4o

# Gemini AI (para procesamiento de media)
GEMINI_API_KEY=AIzaSy...

# Gupshup (para notificaciones WhatsApp)
GUPSHUP_NOTIF_API_KEY=sk_...
GUPSHUP_NOTIF_SOURCE=573001234567
GUPSHUP_ESCALATION_TEMPLATE_ID=uuid-del-template
GUPSHUP_NOTIF_APP_NAME=nombre-de-tu-app
```

#### Variables Opcionales (Recomendadas para Producción):

```env
# Redis (para persistencia de estado)
REDIS_URL=redis://default:password@host:port

# Langfuse (observabilidad)
LANGFUSE_HOST=https://cloud.langfuse.com
LANGFUSE_PUBLIC_KEY=pk_lf_...
LANGFUSE_SECRET_KEY=sk_lf_...

# Webhook Secret
WEBHOOK_SECRET=tu-webhook-secret-seguro
```

### 3. Configuración de Puertos

El sistema expone los siguientes puertos:

- **8080**: Express Gateway (para webhooks de Gupshup)
- **8000**: Python Service (API interna)

En Easypanel, asegúrate de:
1. Exponer el puerto 8080 públicamente para recibir webhooks
2. Configurar el dominio/subdomain para tu aplicación

### 4. Configuración del Webhook en Gupshup

Una vez desplegado, configura el webhook en Gupshup:

1. Ve a tu dashboard de Gupshup
2. Configura el webhook URL: `https://tu-dominio.easypanel.app/webhooks/gupshup`
3. Asegúrate de que el webhook esté activo

### 5. Verificación del Despliegue

Para verificar que todo está funcionando:

1. **Health Check del Gateway**:
   ```
   curl https://tu-dominio.easypanel.app/
   ```
   Debe responder: `SkytideCRM Express Gateway is running`

2. **Health Check del Python Service**:
   ```
   curl https://tu-dominio.easypanel.app:8000/
   ```
   Debe responder con información del servicio

3. **Revisar logs en Easypanel**:
   - Verifica que ambos servicios (`express-gateway` y `python-service`) estén ejecutándose
   - Revisa los logs para cualquier error de configuración

### 6. Consideraciones de Seguridad

- **Nunca** expongas las variables de entorno sensibles en el repositorio
- Usa secretos de Easypanel para las API keys
- Configura `WEBHOOK_SECRET` para validar los webhooks entrantes
- Asegúrate de usar HTTPS para todos los endpoints públicos

### 7. Monitoreo y Mantenimiento

- Configura alertas en Easypanel para reiniciar servicios si fallan
- Revisa los logs regularmente
- Si configuras Langfuse, úsalo para monitorear el rendimiento del agente AI
- Configura backups de tu base de datos Supabase

## Solución de Problemas Comunes

### Error: "Cannot connect to Supabase"
- Verifica que las credenciales de Supabase sean correctas
- Asegúrate de usar `SUPABASE_SERVICE_ROLE_KEY` y no la anon key para el servicio Python

### Error: "OpenAI API key invalid"
- Verifica que tu API key de OpenAI esté activa y tenga créditos
- Asegúrate de que el modelo configurado (`gpt-4o`) esté disponible en tu cuenta

### Error: "Webhook not receiving messages"
- Verifica que el puerto 8080 esté expuesto públicamente
- Confirma que la URL del webhook en Gupshup sea correcta
- Revisa los logs del `express-gateway` para ver si llegan las peticiones

### Error: "Redis connection failed"
- Si no tienes Redis configurado, el sistema usará memoria local (no recomendado para producción)
- Para producción, considera usar un servicio Redis gestionado

## Soporte

Para problemas específicos del código, revisa:
- Los logs en Easypanel
- La documentación en `/CLAUDE.md`
- El repositorio en GitHub para reportar issues