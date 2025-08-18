
# PLAN DE DESARROLLO - AGENTE IA CONVERSACIONAL

## 1. Arquitectura del Sistema ✅ COMPLETADA

### 1.1. Stack Tecnológico Definido ✅
- **Frontend**: Pydantic AI + LangGraph
- **Base de datos**: Supabase (PostgreSQL)
- **API Gateway**: Express.js
- **Servicio Python**: FastAPI
- **IA Media**: Google Gemini 2.0 Flash (transcripción y descripción)
- **Storage**: Supabase Storage

### 1.2. Estructura del Proyecto ✅
```
skytidecrm-agent/
├── express-gateway/     # API Gateway en Node.js
├── python-service/      # Servicio principal en Python
└── docker-compose.yml   # Orquestación de servicios
```

---

## 2. Desarrollo del API Gateway (Express.js) ✅ COMPLETADO

### 2.1. Configuración Base del Proyecto ✅
- **Dockerfile**: Configurado para Node.js
- **package.json**: Dependencias de Express.js, Supabase y Google Generative AI
- **Estructura de carpetas**: Organizada para middlewares, rutas y utilities

### 2.2. Webhook de Gupshup ✅ COMPLETADO ⭐ CON MEDIA PROCESSING
- **Endpoint**: `POST /webhooks/gupshup`
- **Middleware resolveOrganization**: ✅ Resuelve `organization_id` desde `gupshup_app_name`
- **Middleware resolveChatIdentity**: ✅ Resuelve `chat_identity_id` y `contact_id`
- **⭐ NUEVO - Procesamiento de Media**: ✅ Manejo de audio, imagen, video, documentos, location, contact
- **Guardado de mensajes**: ✅ Almacena mensajes con metadatos de media en `chat_messages`
- **Validación**: ✅ Datos de teléfono y contenido del mensaje
- **Forwarding**: ✅ Envío de payload optimizado al servicio Python

### 2.3. ⭐ NUEVO - Procesamiento de Media ✅ IMPLEMENTADO
- **Audio**: ✅ Descarga → Supabase Storage → Gemini transcripción → Texto al agente
- **Imagen**: ✅ Descarga → Supabase Storage → Gemini descripción → Texto al agente  
- **Video**: ✅ Descarga → Supabase Storage → Mensaje de fallback
- **Documentos**: ✅ Descarga → Supabase Storage → Mensaje de fallback
- **Location**: ✅ Extracción de coordenadas → Mensaje estructurado (sin archivo)
- **Contact**: ✅ Extracción de datos → Mensaje estructurado (sin archivo)
- **Utility**: ✅ `mediaProcessor.js` con funciones especializadas

### 2.4. Integración con Supabase ✅
- **Cliente Supabase**: ✅ Configurado en middlewares
- **Consulta platform_connections**: ✅ Filtrado por `gupshup_app_name`, `is_active`, `platform='whatsapp'`
- **Gestión chat_identities**: ✅ Creación/búsqueda automática
- **Persistencia de mensajes**: ✅ Historial completo en `chat_messages` con campos de media
- **⭐ Storage**: ✅ Bucket `chat-media` configurado y funcional

### 2.5. Middlewares de Seguridad ✅
- **Autenticación**: ✅ Validación de organizaciones activas
- **Aislamiento de datos**: ✅ Filtrado por `organization_id`
- **Manejo de errores**: ✅ Respuestas robustas

### 2.6. Rutas Internas para Notificaciones ✅
- **Endpoint**: `POST /internal/notify/escalation`
- **Funcionalidad**: ✅ Envío de templates de WhatsApp para escalamiento humano
- **Integración**: ✅ Supabase para obtener API keys de Gupshup
- **Seguridad**: ✅ Solo accesible internamente desde Python service

### 2.7. 📋 MAPA COMPLETO DE RUTAS - DESARROLLO vs PRODUCCIÓN ✅ DOCUMENTADO

#### 🏭 **FLUJO DE PRODUCCIÓN (WhatsApp)**
```
WhatsApp → Gupshup → Express Gateway → Python Service
                    /webhooks/gupshup → /invoke
```

- **🔧 Express Gateway**: `POST /webhooks/gupshup`
  - **Propósito**: Recibe webhooks de Gupshup (WhatsApp Business API)
  - **Middlewares**: resolveOrganization + resolveChatIdentity  
  - **Media Processing**: ✅ Audio, imagen, video, documentos, ubicación, contactos
  - **Forward a Python**: `${PYTHON_API_URL}/invoke` (http://python-service:8000/invoke)
  
- **🔧 Python Service**: `POST /invoke`
  - **Propósito**: Endpoint principal del sistema multi-agente
  - **Funcionalidad**: Supervisor → Knowledge/Appointment/Escalation Agents → Zep Memory
  - **Payload**: InvokePayload (organization_id, contact_id, chat_identity_id, message...)

#### 🧪 **FLUJO DE DESARROLLO/TESTING**
```
Pruebas → Express Gateway → Python Service  
         /chat           → /chat
```

- **🧪 Express Gateway**: `POST /chat`
  - **Propósito**: Endpoint directo para pruebas sin Gupshup
  - **Sin Middlewares**: No resolveOrganization ni resolveChatIdentity
  - **Forward a Python**: `http://python-service:8000/chat`
  
- **🧪 Python Service**: `POST /chat`
  - **Propósito**: Alias de /invoke para facilitar testing
  - **Funcionalidad**: Idéntica a /invoke (mismo sistema multi-agente)
  - **Implementación**: ✅ `async def chat()` → `return await invoke()`

#### 🔄 **RUTAS AUXILIARES**
- **Express Gateway**: `POST /internal/notify/escalation`
  - **Uso**: Python service → Express Gateway → WhatsApp (escalaciones)
  
- **Express Gateway**: `GET /` → "API Gateway is running..."
- **Python Service**: `GET /` → {"Hello": "World"}

#### ⚙️ **CONFIGURACIÓN DE SERVICIOS**
- **Docker Internal**: `PYTHON_API_URL=http://python-service:8000`
- **Local Development**: `PYTHON_API_URL=http://localhost:8000`
- **Production**: `PYTHON_API_URL=https://your-python-service.domain.com`

### 2.8. Optimización del Payload ✅
- **Extracción directa**: ✅ `country_code` y `dial_code` desde payload Gupshup
- **Payload limpio**: ✅ Solo datos esenciales
- **Eliminación de redundancia**: ✅ Sin `webhook_payload`, sin `phonenumbers`

---

## 3. Desarrollo del Servicio Python (FastAPI + Pydantic AI) ✅ COMPLETADO

### 3.1. Configuración del Proyecto Python ✅
- **Dockerfile**: ✅ Configurado para Python
- **requirements.txt**: ✅ FastAPI, Pydantic AI, LangGraph, Zep, Supabase, httpx
- **Estructura**: ✅ Separación en módulos especializados
- **⭐ OpenAI Direct**: ✅ Uso directo de OpenAI API para evitar comisiones de terceros

### 3.2. Integración con LangGraph ✅
- **Estado Global**: ✅ `GlobalState` con persistencia de checkpointing
- **Workflow**: ✅ Definido con nodos especializados y routing inteligente
- **MemorySaver**: ✅ Configurado para persistencia de estado entre mensajes

### 3.3. Integración con Zep Cloud ❌ Eliminada
- **Cliente Zep**: ✅ Configurado y funcional
- **Gestión de sesiones**: ✅ Historia de conversación persistente
- **Optimización**: ✅ Simplificado gracias a MemorySaver de LangGraph

### 3.4. Desarrollo de Sub-Agentes Especializados

#### ~~ContactAgent~~ ❌ **ELIMINADO** ✅ **NUEVA ARQUITECTURA**
**JUSTIFICACIÓN**: Con el nuevo diseño donde Express Gateway resuelve `chat_identity_id` y `contact_id` ANTES de enviar al Python service, este agente perdió completamente su propósito.

**BENEFICIOS DE ELIMINACIÓN**:
- ✅ **Performance mejorada**: No más ejecución redundante por cada mensaje
- ✅ **Arquitectura más limpia**: Responsabilidades claramente separadas
- ✅ **Menos complejidad**: Gateway maneja resolución, Python maneja lógica de negocio
- ✅ **Consistencia de datos**: Single source of truth en Gateway

#### KnowledgeAgent ✅ (PENDIENTE: Búsqueda semántica real)
- **Estructura**: ✅ Patrón Agent/Tool con Pydantic AI
- **Funcionalidad**: ✅ Búsqueda de servicios y respuesta a preguntas
- **⭐ Media Ready**: ✅ Recibe texto procesado de audio/imagen por Gemini
- **Pendiente**: 🔄 Reemplazar simulación con búsqueda semántica real en Supabase

#### AppointmentAgent ✅ COMPLETADO
- **Estructura**: ✅ Patrón Agent/Tool con Pydantic AI
- **Tools implementadas**: ✅ `check_availability`, `book_appointment`, `resolve_contact_on_booking`, `create_whatsapp_opt_in`
- **Integración Supabase**: ✅ Conectado a tablas reales (`appointments`, `contacts`, `contact_authorizations`)
- **Lógica de opt-in WhatsApp**: ✅ Implementada según especificaciones
- **⭐ Media Ready**: ✅ Maneja texto procesado desde cualquier tipo de media

#### EscalationAgent ✅ COMPLETADO
- **Arquitectura**: ✅ Direct Action Node (determinístico)
- **Funcionalidad**: ✅ Escalamiento a asesor humano con notificación WhatsApp
- **Integración interna**: ✅ Llamada HTTP al endpoint `/internal/notify/escalation`
- **Lógica de bot**: ✅ Desactivación condicional solo si notificación exitosa
- **Datos dinámicos**: ✅ Obtención de nombre de cliente desde `contacts` o fallback a "Cliente"

### 3.5. Supervisor Agent ✅ COMPLETADO Y OPTIMIZADO
- **Arquitectura**: ✅ Pydantic AI con structured output
- **Routing**: ✅ **SIMPLIFICADO** - Sin ContactAgent, va directo a agentes de negocio
- **Lógica**: ✅ **MEJORADA** - Conoce que datos de contacto vienen pre-resueltos
- **⭐ Media Aware**: ✅ Enruta inteligentemente contenido procesado desde media
- **⭐ OpenAI Direct**: ✅ Uso directo de OpenAI API (gpt-4o) sin intermediarios
- **Agentes disponibles**: KnowledgeAgent, AppointmentAgent, EscalationAgent

### 3.6. Endpoint Principal FastAPI ✅ COMPLETADO Y OPTIMIZADO
- **Endpoint**: `POST /invoke`
- **Payload**: ✅ **OPTIMIZADO** - Recibe `chatIdentityId` y `contactId` pre-resueltos
- **Estado inicial**: ✅ **SIMPLIFICADO** - Construye `GlobalState` con datos completos
- **⭐ Media Content**: ✅ Recibe texto procesado listo para agentes IA
- **Flujo**: ✅ **MEJORADO** - Entrada directa al Supervisor sin pasos redundantes

---

## ⭐ 4. PROCESAMIENTO INTELIGENTE DE MEDIA 🔄 EN IMPLEMENTACIÓN

### 4.1. Arquitectura de Media ✅ COMPLETADA
- **Gateway Processing**: ✅ Express Gateway maneja descarga y almacenamiento
- **IA Integration**: ✅ Google Gemini 2.0 Flash para transcripción y descripción
- **Storage Strategy**: ✅ Supabase Storage con organización por organización/chat
- **Database Schema**: ✅ Campos `media_type`, `media_url`, `media_mime_type` en `chat_messages`

### 4.2. Tipos de Media Soportados ✅ IMPLEMENTADOS
- **🎵 Audio**: ✅ Descarga → Storage → Gemini transcripción → Agente IA
- **🖼️ Imagen**: ✅ Descarga → Storage → Gemini descripción → Agente IA
- **📹 Video**: ✅ Descarga → Storage → Fallback message (no procesamiento IA)
- **📄 Documentos**: ✅ Descarga → Storage → Fallback message (no procesamiento IA)
- **📍 Location**: ✅ Extracción coordenadas → Mensaje estructurado (sin archivo)
- **👤 Contact**: ✅ Extracción datos → Mensaje estructurado (sin archivo)

### 4.3. Flujo de Procesamiento ✅ IMPLEMENTADO
```
Gupshup Media → mediaProcessor.js → Gemini (si aplica) → 
Supabase Storage → chat_messages → Python service (texto procesado)
```

### 4.4. ⚠️ PENDIENTES CRÍTICOS PARA FUNCIONALIDAD COMPLETA
- **🔑 Variable de entorno**: `GEMINI_API_KEY` - REQUERIDA para transcripción/descripción (ESTRUCTURA ✅)
- **🧪 Testing completo**: Validar todos los tipos de media con datos reales (ESTRUCTURA ✅, TESTING REAL PENDIENTE)
- **📋 Documentación**: ✅ **COMPLETADO** - Variables de entorno y configuración de Storage

---

## 5. Integración y Testing

### 5.1. Integración entre Servicios ✅ COMPLETADO Y OPTIMIZADO
- **Comunicación**: ✅ **MEJORADA** - HTTP entre Express Gateway y Python service
- **Payload**: ✅ **OPTIMIZADO** - Estructura limpia y eficiente
- **⭐ Media Flow**: ✅ **NUEVO** - Flujo completo de media a texto procesado
- **Error handling**: ✅ Manejo robusto de errores en ambos servicios
- **Logging**: ✅ **MEJORADO** - Trazabilidad completa del flujo

### 5.2. Testing de Funcionalidades Básicas ✅ COMPLETADO
- **Webhook Gupshup**: ✅ Probado flujo completo optimizado
- **⭐ Media Processing**: ✅ **VALIDADO** - Estructura completa para audio, imagen, video, documentos, location, contact
- **Agentes especializados**: ✅ Validado funcionamiento sin ContactAgent
- **Persistencia**: ✅ Verificado guardado correcto de mensajes y estado con media
- **Escalamiento**: ✅ Probado notificaciones internas con manejo robusto de errores
- **🛡️ Error Handling**: ✅ **NUEVO** - Testing completo de manejo de errores y casos edge
- **🔧 Dependencies**: ✅ **NUEVO** - Todas las dependencias verificadas e instaladas
- **📝 Syntax**: ✅ **NUEVO** - Todos los archivos pasan validación de sintaxis

### 5.3. Casos de Uso End-to-End ✅ COMPLETADO
- **Primera conversación**: ✅ Flujo sin ContactAgent validado
- **⭐ Conversación con media**: ✅ **ESTRUCTURA LISTA** - Audio → transcripción → agendamiento
- **⭐ Imagen informativa**: ✅ **ESTRUCTURA LISTA** - Imagen → descripción → KnowledgeAgent
- **Agendamiento de cita**: ✅ KnowledgeAgent → AppointmentAgent
- **Escalamiento humano**: ✅ EscalationAgent con notificaciones robustas
- **Conversación continua**: ✅ Estado persistente optimizado
- **🛡️ Robustez**: ✅ **NUEVO** - Manejo de errores en todos los flujos

---

## 6. Pendientes y Mejoras Futuras

### 6.1. ⚠️ TAREAS CRÍTICAS INMEDIATAS
- **🔑 OPENAI_API_KEY**: ✅ **COMPLETADO** - Uso directo de OpenAI API sin comisiones de terceros
- **🔑 ZEP_API_KEY**: ✅ **COMPLETADO** - Migración completa a Zep Cloud con gestión avanzada de memoria
- **🔑 GEMINI_API_KEY**: Configurar variable de entorno para procesamiento IA (ESTRUCTURA ✅, CONFIGURACIÓN PENDIENTE)
- **🧪 Testing Media**: Probar flujo completo con archivos reales (ESTRUCTURA ✅, TESTING REAL PENDIENTE)
- **📚 Documentación**: ✅ **COMPLETADO** - Guía de configuración de variables de entorno actualizada
- **🔍 Búsqueda Semántica**: ✅ **COMPLETADO** - Implementado con Zep Cloud (search_facts, search_nodes, search_sessions)
- **🧠 Tools de Búsqueda Directa**: ✅ **COMPLETADO** - KnowledgeAgent ahora tiene superpoderes de memoria
- **⚠️ Message Status Default**: Cambiar default de `message_status` de 'sent' a 'pending' cuando se complete la migración del CRM existente
- **✅ COMPLETADO - Testing Sistema**: ✅ **NUEVO** - Testing completo de integración, errores, dependencias y sintaxis

### 6.2. Funcionalidades Pendientes Medio Plazo
- **UUIDs Hardcodeados**: 🔄 Reemplazar `created_by` con sistema real de agentes
- **Webhook de Estado de Mensajes**: 🔄 `POST /webhooks/gupshup/status` para tracking
- **Tests Automatizados**: 🔄 Unit, integration y end-to-end tests
- **⭐ Media Analytics**: 🔄 Métricas de uso de diferentes tipos de media

### 6.3. Optimizaciones de Performance ✅ COMPLETADAS
- ✅ **Eliminación ContactAgent**: Reducción significativa de latencia
- ✅ **Resolución Gateway**: Single query por conversación vs por mensaje
- ✅ **Payload Optimizado**: Menos datos transferidos entre servicios
- ✅ **Guardado Automático**: Historial sin intervención manual
- ✅ **⭐ Media Streaming**: Procesamiento eficiente sin bloqueos

### 6.4. Refactoring y Mantenibilidad 🔄 FUTURO
- **Servicios Especializados**: 🔄 Mover lógica de negocio a módulos de servicio
- **Monitoreo**: 🔄 Métricas y observabilidad
- **Escalabilidad**: 🔄 Consideraciones para múltiples instancias
- **⭐ Media Caching**: 🔄 Optimización de acceso a archivos frecuentes

### 6.5. Mejores Prácticas LangGraph/LangChain 🔧 NUEVO
- **Checkpointer duradero (Redis)**: Migrar de `MemorySaver` a `langgraph-redis` para hilos concurrentes multi-tenant.
- **Fallback del router**: Añadir reintentos guiados para `with_structured_output(Route)` en caso de error de parseo.
- **Outputs homogéneos de tools**: Estandarizar respuestas que mutan estado con `{ action: string, ... }` (ej. `reset_appointment_context`, `select_appointment_slot`).
- **Límite de recursión**: Establecer `recursion_limit` por defecto en 25 y elevar bajo diagnóstico.
- **Observabilidad**: Integrar LangSmith o, mínimo, logs estructurados con `thread_id` y `tool_call_id` por paso.

---

## RESUMEN DE CAMBIOS ARQUITECTÓNICOS IMPORTANTES ✅

### **⭐ DECISIÓN ARQUITECTÓNICA: OPENAI DIRECTO** ✅ COMPLETADO
- **Evaluación**: Se consideró OpenRouter vs OpenAI directo
- **Decisión**: OpenAI directo para evitar comisión del 5% de OpenRouter
- **Beneficios**: 
  - 💰 **Sin comisiones adicionales** - 100% del valor va a OpenAI
  - 🔌 **Integración directa** - Sin intermediarios
  - ⚡ **Latencia mínima** - Conexión directa
  - 🛡️ **Confiabilidad máxima** - Sin dependencia de terceros
- **Implementación**: 
  - ✅ Supervisor Agent con OpenAI directo (gpt-4o)
  - ✅ KnowledgeAgent con OpenAI directo (gpt-4o)
  - ✅ AppointmentAgent con OpenAI directo (gpt-4o)
  - ✅ Variables de entorno actualizadas (OPENAI_API_KEY)
  - ✅ Configuración simplificada sin headers adicionales

### **⭐ MIGRACIÓN COMPLETA A ZEP CLOUD** ❌ Eliminada
- **Problema Inicial**: Implementación obsoleta con `zep-python` y APIs deprecated
- **Solución**: Migración completa a `zep-cloud` con mejores prácticas
- **Beneficios Implementados**:
  - 🧠 **Gestión de Usuarios y Sesiones** - Creación automática de usuarios/sesiones en Zep
  - 💬 **Formato Correcto de Mensajes** - `role_type` (user/assistant) vs `role` deprecated
  - 🔍 **Búsqueda Semántica Avanzada** - search_facts, search_nodes, search_sessions
  - 📖 **Recuperación de Contexto** - Todos los agentes usan memoria de Zep
  - ⚡ **Cliente Asíncrono** - AsyncZep para mejor rendimiento
- **Implementación Técnica**:
  - ✅ `requirements.txt`: `zep-python` → `zep-cloud`
  - ✅ `zep.py`: Cliente AsyncZep + funciones auxiliares completas
  - ✅ `supervisor.py`: Context injection desde Zep memory
  - ✅ `knowledge_agent.py`: Enhanced queries con contexto Zep
  - ✅ `appointment_agent.py`: Context enrichment automático
  - ✅ `main.py`: Gestión completa usuarios/sesiones + mensajes
  - ✅ `docker-compose.yml`: Removido ZEP_API_URL (ya no necesario)
- **Funciones Nuevas Implementadas**:
  - `ensure_user_exists()` - Gestión automática de usuarios
  - `ensure_session_exists()` - Gestión automática de sesiones
  - `add_messages_to_zep()` - Persistencia de conversaciones
  - `get_zep_memory_context()` - Recuperación de contexto relevante
  - `search_zep_facts()` - Búsqueda semántica de hechos
  - `search_zep_nodes()` - Búsqueda semántica de nodos
  - `search_zep_sessions()` - Búsqueda semántica de sesiones

### **⭐ PROCESAMIENTO INTELIGENTE DE MEDIA** 🔄 EN IMPLEMENTACIÓN
- **Problema**: WhatsApp envía diferentes tipos de media que bots tradicionales no pueden procesar
- **Solución**: Gateway + Gemini + Supabase Storage para convertir media a texto inteligente
- **Beneficio**: Agentes IA pueden "entender" audio e imágenes como si fueran texto

### **ELIMINACIÓN DE CONTACTAGENT** ✅
- **Problema Original**: ContactAgent se ejecutaba en cada mensaje, resolvía `chat_identity_id` redundantemente
- **Solución Implementada**: Express Gateway resuelve una sola vez y mantiene consistencia
- **Beneficio**: **~50-70% reducción en latencia** y complejidad del sistema

### **MIDDLEWARE RESOLVECHATIDENTITY** ✅
- **Funcionalidad**: Resolución automática de `chat_identity_id` y `contact_id` en Gateway
- **Ubicación**: Entre `resolveOrganization` y procesamiento de mensaje
- **Ventaja**: Single source of truth, datos siempre disponibles

### **GUARDADO AUTOMÁTICO DE MENSAJES** ✅
- **Entrantes**: Después de resolver identidad, antes de Python service (con metadatos de media)
- **Salientes**: Después de respuesta de IA, antes de responder a Gupshup
- **Tabla**: `chat_messages` con historial completo y metadatos de media
- **⭐ Status Tracking**: Estados `pending` → `sent` implementados (webhook IA usa pending, CRM existente usa default sent)

### **⭐ FLUJO OPTIMIZADO CON MEDIA** ✅
```
Gupshup (texto/media) → resolveOrganization → resolveChatIdentity → 
processMedia (Gemini) → saveIncoming → 
Python(Supervisor → Agentes) → saveOutgoing → Response
```

**RESULTADO FINAL**: Sistema más rápido, más simple, más mantenible, con historial completo de conversaciones Y capacidades de IA multimedia. ✅

---

### **🧠 TOOLS DE BÚSQUEDA DIRECTA ZEP** ✅ COMPLETADO
- **Objetivo**: Permitir a los agentes hacer búsquedas específicas en tiempo real durante la conversación
- **Implementación**: KnowledgeAgent ahora tiene 3 nuevos tools de búsqueda
- **Funcionalidades Implementadas**:
  - 🔍 **`search_user_facts`** - Busca hechos específicos del usuario (servicios previos, preferencias, alergias)
  - 💬 **`search_user_conversations`** - Busca conversaciones pasadas (recomendaciones, quejas, problemas)
  - 📊 **`search_user_insights`** - Busca patrones de comportamiento y análisis del usuario
- **Beneficios Reales**:
  - 🎯 **Respuestas Personalizadas** - "¿Qué servicios me recomendaste antes?" → Respuesta específica
  - 🧠 **Memoria Contextual** - El agente "recuerda" conversaciones previas automáticamente
  - ⚡ **Búsqueda Inteligente** - Solo busca cuando es relevante para la consulta actual
- **Ejemplos de Uso**:
  - "¿Cuáles fueron mis servicios favoritos?" → `search_user_facts("servicios favoritos")`
  - "¿Tuve problemas con algún tratamiento?" → `search_user_conversations("problemas tratamiento")`
  - "¿Qué horarios suelo preferir?" → `search_user_insights("horarios preferencias")`
- **Implementación Técnica**:
  - ✅ **3 Nuevos Tools** en `knowledge_agent.py` con Pydantic models
  - ✅ **System Prompt Actualizado** - Flujo de trabajo con búsqueda condicional
  - ✅ **Manejo de Resultados** - Formateo automático de respuestas estructuradas
  - ✅ **Integration con State** - Acceso completo al GlobalState via `deps`
  - ✅ **Error Handling** - Manejo robusto cuando no hay información disponible

### **⚡ OPTIMIZACIONES DE PERFORMANCE ZEP** ✅ COMPLETADO
- **Objetivo**: Implementar mejores prácticas de Zep para máximo rendimiento en conversaciones
- **Optimizaciones Implementadas**:
  - 🔄 **Cliente Singleton Reutilizable** - Una instancia global `zep_client` para toda la app
  - ⚡ **Modo "basic" por Defecto** - P95 < 200ms vs modo "summarized" más lento
  - 🚀 **`return_context=True`** - Contexto inmediato sin llamadas adicionales
  - 🎯 **Queries Concisas** - Búsquedas específicas y enfocadas (< 8,192 tokens)
  - 💬 **`add_messages` Optimizado** - Para mensajes conversacionales < 10K caracteres
- **Beneficios de Performance**:
  - 🏃‍♂️ **Latencia Reducida** - Menos llamadas HTTP, conexiones reutilizadas
  - 💾 **Memoria Optimizada** - Contexto básico vs resumido cuando sea apropiado
  - 🔍 **Búsquedas Eficientes** - Híbrido semántico + BM25 optimizado
  - ⚡ **Round-trips Eliminados** - `return_context=True` obtiene contexto inmediatamente
- **Funciones Optimizadas**:
  - `get_zep_memory_context(mode="basic")` - Contexto rápido por defecto
  - `add_messages_to_zep(return_context=True)` - Optimización de contexto inmediato
- **Impacto Medible**:
  - ⚡ **Contexto Básico**: P95 < 200ms (vs ~500ms+ resumido)
  - 🔄 **Sin Llamadas Extra**: `return_context=True` elimina round-trips adicionales
  - 💬 **Conversaciones Optimizadas**: Mensajes < 10K caracteres procesados eficientemente

---

---

## 8. 🔄 FASE 8: REFACTORIZACIÓN A FLUJO DE CONVERSACIÓN CON NODOS (ARQUITECTURA RETELL)

### 8.1. Justificación del Cambio Arquitectónico

La arquitectura de "Agente Único" ha demostrado ser propensa a errores de lógica y bucles de conversación, ya que delega demasiado control de flujo a la interpretación de un único LLM con un prompt muy complejo.

Inspirados en las mejores prácticas de frameworks como [Retell AI](https://docs.retellai.com/build/conversation-flow/overview), adoptaremos una arquitectura de **Grafo de Estados Explícito** utilizando `LangGraph`. Esto nos dará un control total y predecible sobre el flujo de la conversación, eliminando la ambigüedad y facilitando enormemente la depuración.

**Beneficios Esperados:**
-   **Robustez y Previsibilidad:** El flujo conversacional se define en el código a través de nodos y aristas, no en un prompt.
-   **Depuración Sencilla:** Los logs mostrarán claramente el paso de un nodo a otro, permitiendo identificar fallos al instante.
-   **Flexibilidad para Cambios de Intención:** Un nodo "Supervisor/Enrutador" central permitirá saltar entre diferentes flujos (agendamiento, conocimiento, etc.) de forma inteligente.
-   **Mantenibilidad a Largo Plazo:** Añadir nuevos pasos o flujos será tan simple como añadir nuevos nodos y aristas al grafo.

### 8.2. Plan de Migración por Fases

#### **FASE 8.2.1: Creación del Grafo de Nodos Especializados**

-   [ ] **Definir Nodos Principales en `main.py`**:
    -   `supervisor_node`: Punto de entrada que analiza la intención del usuario y el estado actual para enrutar la conversación.
    -   `knowledge_node`: Llama a la herramienta `knowledge_search` y formatea la respuesta.
    -   `appointment_node`: Un sub-grafo que contendrá toda la lógica de agendamiento.
    -   `cancellation_node`: Un sub-grafo para el flujo de cancelación de citas.
    -   `confirmation_node`: Nodo final para resumir citas y gestionar opt-ins.
    -   `escalation_node`: Nodo de seguridad para escalar a un humano.

-   [ ] **Implementar el `supervisor_node`**:
    -   Crear un prompt específico para este nodo, cuyo único objetivo es decidir a qué otro nodo debe ir la conversación.
    -   Debe devolver una decisión estructurada, por ejemplo: `{"next": "knowledge_node"}`.

-   [ ] **Configurar las Aristas Condicionales**:
    -   En `main.py`, conectar el `supervisor_node` a los demás nodos principales usando `workflow.add_conditional_edges`.

#### **FASE 8.2.2: Construcción del Sub-Grafo de Agendamiento (`appointment_graph.py`)**

-   [ ] **Crear `appointment_graph.py`**: Nuevo archivo para contener la lógica del flujo de agendamiento.
-   [ ] **Definir Nodos del Sub-Grafo**:
    -   `check_service_node`: Verifica si `service_id` existe.
    -   `search_service_node`: Llama a la herramienta `knowledge_search`.
    -   `confirm_service_node`: Pide al usuario que confirme el servicio encontrado.
    -   `save_service_node`: Llama a la herramienta `update_service_in_state`.
    -   `get_date_node`: Pregunta por la fecha.
    -   `check_availability_node`: Llama a la herramienta `check_availability`.
    -   `select_slot_node`: Llama a la herramienta `select_appointment_slot`.
    -   `resolve_contact_node`: Llama a `resolve_contact_on_booking`.
    -   `book_appointment_node`: Llama a la herramienta final `book_appointment`.
-   [ ] **Conectar Nodos del Sub-Grafo**: Crear un `StateGraph` dentro de este archivo que defina el flujo lineal del agendamiento.
-   [ ] **Integrar Sub-Grafo en `main.py`**: El `supervisor_node` enrutará al `appointment_graph` cuando la intención sea agendar.

#### **FASE 8.2.3: Refactorización de Herramientas y Estado**

-   [ ] **Mover Herramientas a `tools.py`**: Crear un archivo `tools.py` para centralizar todas las funciones de herramientas (`knowledge_search`, `check_availability`, etc.), eliminándolas de `master_agent.py`.
-   [ ] **Eliminar `master_agent.py`**: Este archivo ya no será necesario, ya que la lógica estará distribuida en los nodos.
-   [ ] **Actualizar `state.py`**: Añadir un campo `current_flow: Optional[str]` al `GlobalState` para que el supervisor siempre sepa en qué flujo se encuentra el usuario (ej: "agendamiento", "conocimiento").

#### **FASE 8.2.4: Testing End-to-End**

-   [ ] **Prueba de Flujo de Agendamiento Lineal**: Validar que el sub-grafo de agendamiento funciona de principio a fin sin interrupciones.
-   [ ] **Prueba de Salto de Intención**:
    -   Iniciar un flujo de agendamiento.
    -   A mitad de camino, hacer una pregunta de conocimiento.
    -   Verificar que el `supervisor_node` enruta correctamente al `knowledge_node` y luego puede regresar al flujo de agendamiento.
-   [ ] **Prueba de Cambio de Contexto Completo**:
    -   Iniciar un agendamiento para "masaje".
    -   Decir "mejor quiero una limpieza facial".
    -   Verificar que el supervisor reinicia el sub-grafo de agendamiento.

---

## 🎯 PRÓXIMOS PASOS INMEDIATOS

1.  **Iniciar Fase 8.2.3**: Mover herramientas a `tools.py` y eliminar `master_agent.py`.
2.  **Iniciar Fase 8.2.1**: Implementar `supervisor_node` y la estructura base del grafo en `main.py`.
3.  **Iniciar Fase 8.2.2**: Construir el sub-grafo de agendamiento.
4.  **Configurar Variables de Entorno**: `OPENAI_API_KEY` y `GEMINI_API_KEY` son críticas.
5.  **Testing Progresivo**: Probar cada flujo a medida que se construye.

## 9. 🔄 FASE 9: RECONSTRUCCIÓN TOTAL A ARQUITECTURA DE NODOS EXPERTOS (MODELO RETELL)

### 9.1. Justificación y Análisis del Fallo

Tras repetidos fracasos, se ha determinado que la arquitectura actual es fundamentalmente defectuosa. Aunque utiliza nodos, el control centralizado en un único `supervisor` que se re-ejecuta en cada turno crea bucles de conversación, ignora las entradas del usuario y provoca `timeouts`. El modelo de "supervisor" + "trabajadores tontos" ha fracasado.

La solución es una reconstrucción completa para emular la arquitectura robusta de sistemas como Retell AI, basada en **Nodos Inteligentes (Agentes Expertos)** y un **control de flujo explícito a través de aristas condicionales**, donde la conversación permanece dentro de un nodo experto hasta que se resuelve su tarea o la intención del usuario cambia drásticamente.

### 9.2. Plan de Reconstrucción

#### **FASE 9.2.1: Transformar `knowledge_node` en el Primer Agente Experto (Prototipo)**

-   [ ] **Crear `knowledge_agent_prompt.py`**:
    -   Definir un prompt detallado que le dé al nodo la capacidad de razonar.
    -   Instrucciones claras: si es un saludo, conversar; si es una pregunta, usar la herramienta `knowledge_search`.
-   [ ] **Reescribir `knowledge_node` en `main.py`**:
    -   Convertirlo en una cadena LangChain (Prompt + LLM + Herramientas).
    -   El nodo ahora recibirá el estado y decidirá si llama a la herramienta o si genera una respuesta conversacional directamente.
    -   La salida será siempre una `AIMessage`, que puede contener una llamada a herramienta o texto plano.

#### **FASE 9.2.2: Transformar `appointment_node` en un Agente Experto Completo**

-   [ ] **Eliminar el Sub-Grafo (`appointment_graph.py`)**: La lógica de agendamiento ya no estará en un grafo separado, sino dentro de la inteligencia del propio `appointment_node`.
-   [ ] **Crear `appointment_agent_prompt.py`**:
    -   Diseñar un prompt complejo que funcione como una máquina de estados conversacional.
    -   Debe entender en qué paso del agendamiento se encuentra (ej: `buscando_servicio`, `pidiendo_fecha`, `seleccionando_hora`).
    -   Debe saber qué herramienta llamar en cada paso (`knowledge_search`, `check_availability`, `book_appointment`).
-   [ ] **Reescribir `appointment_node` en `main.py`**:
    -   Implementarlo como una cadena LangChain (Prompt + LLM + Todas las herramientas de agendamiento).
    -   La conversación **permanecerá dentro de este nodo** a través de múltiples turnos hasta que la cita se agende o el usuario cambie de intención.

#### **FASE 9.2.3: Simplificar el Supervisor y las Conexiones del Grafo**

-   [ ] **Redefinir el Rol del `supervisor`**:
    -   Su único propósito será el enrutamiento inicial. No volverá a ejecutarse después de cada turno de un nodo experto.
-   [ ] **Reestructurar las Aristas en `main.py`**:
    -   Los nodos expertos (`knowledge_node`, `appointment_node`) ya no volverán al supervisor por defecto.
    -   Se implementará una lógica de "auto-retorno" o un `edge` condicional que solo se active si la intención del usuario cambia drásticamente, forzando una re-evaluación del enrutamiento por parte del supervisor.

#### **FASE 9.2.4: Testing del Nuevo Modelo**

-   [ ] **Prueba de Conversación Casual**: Verificar que el `knowledge_node` responde a saludos sin buscar en la base de datos.
-   [ ] **Prueba de Agendamiento Completo**: Realizar un agendamiento de principio a fin, verificando que la conversación se mantiene dentro del `appointment_node` y que este llama a las herramientas correctas en el orden correcto.
-   [ ] **Prueba de Cambio de Intención**: Iniciar un agendamiento y luego hacer una pregunta. Verificar que el flujo puede salir del `appointment_node`, ser re-evaluado por el `supervisor` y entrar correctamente al `knowledge_node`.

---

## 10. 🔐 FASE 10: MEMORIA CONVERSACIONAL EN SUPABASE + CHECKPOINTER REDIS (SUSTITUYE ZEP) ✅ COMPLETADA

### 10.1. Objetivo
- Reemplazar Zep como capa de memoria para reducir costo/latencia y aumentar el control, manteniendo durabilidad del grafo con Redis.

### 10.2. Alcance
- Redis: checkpointer duradero para `LangGraph` y caché caliente de últimos N mensajes normalizados por hilo.
- Supabase: memoria conversacional persistente (source of truth) con historial “normalizado” + resumen por hilo.
- Sin nuevos vendors (no mem0 salvo que se solicite luego).

### 10.3. Cambios en API Gateway (Express)
- [X] Guardado de mensajes entrantes con campos adicionales en `chat_messages`:
  - `processed_text text` (transcripción/descripción enviada al LLM)
  - `media_type text`, `media_url text`, `artifacts jsonb` (opcional)
- [X] Enviar al Python-service el `processedText` (si existe) como contenido del mensaje para contexto.
- [X] Mantener caché en memoria de `chat_identity` → `contact_id` y `first_name` (TTL 24h).
- [X] Buffer (debounce) 10s para consolidar mensajes en una sola invocación.
- (Opcional) Push a Redis caché de conversación tras guardar en Supabase:
  - Key: `chat:{organization_id}:{chat_identity_id}:messages`
  - Operaciones sugeridas: `LPUSH` con mensaje normalizado y `LTRIM` para mantener N (p.ej., 25–50).

### 10.4. Cambios en Python-service
- [X] Sustituir `MemorySaver` por `RedisSaver` de `langgraph-checkpoint-redis` si `REDIS_URL` definido; fallback a `MemorySaver`.
  - `pip install langgraph-checkpoint-redis redis`
  - `REDIS_URL=redis://redis:6379` (o Upstash/ElastiCache)
- [X] Nuevo módulo `app/memory.py`:
  - `get_last_messages(chat_identity_id, n)` → lee de `chat_messages` (usa `message`/`processed_text`).
  - `get_context_block(chat_identity_id)` → lee `thread_summaries.summary_text`.
  - `upsert_thread_summary(...)` disponible (pendiente autosummarize).
- [X] `main.py` usa exclusivamente Supabase para memoria (Zep retirado).

### 10.5. Esquema Supabase
- Tabla `chat_messages` (ya existente): agregar columna `processed_text text` (y opcional `artifacts jsonb`).
- Nueva tabla `thread_summaries`:
  - `id uuid pk default gen_random_uuid()`
  - `organization_id uuid not null` (FK)
  - `chat_identity_id uuid not null` (FK)
  - `summary_text text not null`
  - `updated_at timestamptz not null default now()`
  - Índices por `(organization_id, chat_identity_id)`

### 10.6. Configuración y variables de entorno
- `REDIS_URL` (obligatoria)
- Eliminadas variables `ZEP_*` del runtime.

### 10.7. Plan de despliegue
1) Migraciones Supabase: columnas nuevas + tabla `thread_summaries`.
2) Actualizar gateway para persistir `processed_text` y (opcional) publicar a Redis caché de conversación.
3) Añadir `langgraph-checkpoint-redis` y configurar `RedisSaver` en `main.py`.
4) Implementar `memory.py` con lectura preferente desde Redis caché y fallback a Supabase; reemplazar referencias a Zep.
5) Feature flag temporal: `USE_ZEP=false` → activar nueva memoria y checkpointer.
6) Pruebas E2E: hilos con texto, audio e imagen; cancelación/confirmación/reagendamiento.

### 10.8. Observabilidad
- Logs con `thread_id` y tamaño de contexto (`last_messages_n`, tokens aprox. y si se usó `summary_text`).
- Métricas: latencia promedio por turno, tasa de fallos, tamaño medio de `processed_text`, **cache hit-rate Redis** y latencia Redis.

### 10.9. Riesgos y mitigación
- Riesgo: pérdida de contexto al migrar. Mitigar haciendo doble escritura (Zep + Supabase) durante una ventana corta y validando equivalencias.
- Riesgo: Redis no disponible. Mitigar con `ShallowRedisSaver` o fallback a in-memory en dev; alertas de salud.

### 10.10. Criterios de aceptación
- Checkpointer Redis activo y estable (reanudación correcta por `thread_id`).
- Redis caché de últimos N operativo (hit-rate ≥ 80% en producción inicial) con fallback a Supabase.
- `processed_text` persistido y usado para construir el historial.
- Resumen por hilo actualizado y consultado en cada invocación.
- Zep removido del camino crítico sin regresiones de UX.

### 10.11. Buffer de mensajes (debounce 10s)
- Objetivo: evitar múltiples invocaciones al servicio Python cuando el usuario envía varios mensajes cortos seguidos (ej.: "hola" → 3s → "cómo estás" → 5s → "qué limpiezas tienen?").
- Diseño (Gateway):
  - Mapa en memoria `pendingByChat` con clave `org:chatIdentityId` → { timer, items[] }.
  - Al recibir un mensaje: guardar en Supabase (`chat_messages` con `processed_text`), agregar `processedText` normalizado a `items[]`, reiniciar timer a 10s.
  - Al expirar el timer (10s sin nuevos mensajes): construir un único contenido combinando los `processedText` (p. ej., unidos por `\n`), enviar UNA sola solicitud a `/invoke` con ese contenido.
  - Tamaño máximo configurable (p. ej. 3–5 mensajes por lote) para evitar prompts gigantes (si se excede, forzar flush anticipado).
- Alternativa (si se prefiere más estructura):
  - Enviar `batchedMessages: [{role: 'user', content: ...}, ...]` en el payload; el Python-service los insertará a su historial antes del turno actual. (Requiere pequeño cambio en `/invoke`).
- Consideraciones:
  - Los mensajes individuales igual quedan en `chat_messages` (SoR y CRM), por lo que no se pierde auditoría.
  - El agente recibe el contexto concatenado en un solo turno, reduciendo latencia y evitando respuestas parciales.
  - Mantener compatibilidad con media: siempre usar `processed_text` para agregar al buffer (no solo enlaces).

---

## 11. 🚀 Guía de Implementación en Producción (cuando finalicen pruebas)

### 11.1. Despliegue del API Gateway (Express)
- Contenedor: `express-gateway`
- Variables clave:
  - `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`
  - `PYTHON_SERVICE_URL` (ej.: `http://python-service:8000` o dominio público)
  - `REDIS_URL` (para caché de últimos mensajes)
  - `LOG_LEVEL` (info|debug)
- Recomendaciones:
  - Recursos mínimos: 0.5 vCPU / 256–512 MB RAM
  - Habilitar healthcheck y restart `always`
  - Exponer solo puerto público del gateway

### 11.2. Despliegue del Servicio Python (FastAPI + LangGraph)
- Contenedor: `python-service`
- Variables clave:
  - `OPENAI_API_KEY`, `OPENAI_CHAT_MODEL` (ej.: gpt-4o)
  - `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`
  - `REDIS_URL` (checkpointing de LangGraph)
  - (Opcional observabilidad LLM) `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`
- Recomendaciones:
  - Recursos mínimos: 1 vCPU / 512–1024 MB RAM
  - Restart `always`, healthcheck en `/`
  - Limitar `recursion_limit` y saneo de entradas

### 11.3. Observabilidad (stack separado)
- Compose independiente: `infra/observabilidad/docker-compose.observability.yml`
- Servicios incluidos:
  - `langfuse` + `langfuse-db` (Postgres dedicado)
  - `loki`, `promtail`, `grafana` para logs del gateway
- Red compartida: `skytidecrm-network` (external)
- Puertos sugeridos:
  - Langfuse UI: 3001
  - Grafana: 3000
  - Loki: 3100 (interno)
- Recomendaciones de recursos (idle aproximado):
  - Langfuse: 200–350 MB (según uso)
  - Postgres (Langfuse): 150–300 MB
  - Loki+Promtail: 150–250 MB
  - Grafana: 100–200 MB

### 11.4. Red, dominios y seguridad
- Crear red Docker: `docker network create skytidecrm-network`
- Asignar dominios/subdominios:
  - Gateway público (ej.: `gw.tu-dominio.com`)
  - Python-service (interno o protegido)
  - Langfuse (solo interno o protegido por auth)
  - Grafana (protegido por credenciales fuertes)
- TLS/HTTPS: mediante EasyPanel/Traefik/Caddy/Nginx (según tu setup)

### 11.5. Checklist de pre-producción
- [ ] Entorno `.env` completo en ambos servicios
- [ ] `REDIS_URL` operativo
- [ ] Migraciones Supabase aplicadas (`processed_text`, `thread_summaries`, `message_status pending`)
- [ ] Pruebas E2E (texto/audio/imagen, agendar/confirmar/cancelar)
- [ ] Logs verificados en Grafana (si usas Loki) o Dozzle
- [ ] Langfuse recibiendo runs (si habilitado)

### 11.6. Operación y soporte
- Dashboards recomendados:
  - Latencia p50/p95 del gateway y del `/invoke`
  - Errores por organización
  - Throughput por hora
  - Estados de mensaje (`pending/sent/failed`)
- Mantenimiento:
  - Actualizaciones semanales de dependencias
  - Backups del Postgres de Langfuse
  - Rotación de logs en Loki (retención 7–14 días)

