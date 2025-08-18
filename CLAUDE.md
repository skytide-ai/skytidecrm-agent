# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Architecture Overview

This is a **SkytideCRM AI Agent** system for WhatsApp-based appointment booking and customer service automation. The system uses a **microservices architecture** with two main components:

### Core Architecture

**Express.js API Gateway** (`express-gateway/`) - Handles WhatsApp webhooks, media processing, authentication, and internal notifications
**FastAPI Python Service** (`python-service/`) - Contains the main AI agent system built with LangGraph, OpenAI, and Supabase

The system follows a **LangGraph state machine pattern** with specialized expert nodes:
- `supervisor_node`: Routes conversations to appropriate specialist nodes
- `knowledge_node`: Handles general inquiries and service information
- `appointment_node`: Manages appointment booking flow
- `cancellation_node`: Handles appointment cancellations
- `confirmation_node`: Manages appointment confirmations  
- `reschedule_node`: Handles appointment rescheduling
- `escalation_node`: Escalates to human advisors

### Data Flow

```
WhatsApp → Gupshup → Express Gateway → Python Service (LangGraph)
                    ↓ (media processing)
                    Gemini AI (transcription/description)
                    ↓ (storage)
                    Supabase Storage + Database
```

### Memory & State Management

**Conversation Memory**: Supabase (`chat_messages` table with `processed_text` for AI-processed media content)
**State Persistence**: MemorySaver checkpointer (in-memory for development)
**Multi-tenant**: Organization-based data isolation

## Common Development Commands

### Starting the System
```bash
# Start all services
docker-compose up -d

# View logs
docker-compose logs -f
docker-compose logs -f python-service
docker-compose logs -f express-gateway

# Rebuild after code changes
docker-compose up --build -d
```

### Development Workflows
```bash
# Check service status
docker-compose ps

# Restart specific service
docker-compose restart python-service
docker-compose restart express-gateway

# Stop all services
docker-compose down
```

### Node.js Gateway Development
```bash
cd express-gateway
npm install
npm start      # Production
npm run dev    # Development with nodemon
```

### Python Service Development
```bash
cd python-service
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## Environment Configuration

### Required Environment Variables
```bash
# Supabase
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your-service-role-key
SUPABASE_ANON_KEY=your-anon-key

# OpenAI
OPENAI_API_KEY=your-openai-api-key
OPENAI_CHAT_MODEL=gpt-4o  # Optional, defaults to gpt-4o

# Gemini AI (for media processing)
GEMINI_API_KEY=your-gemini-api-key

# Redis (optional, for production checkpointing)
REDIS_URL=redis://localhost:6379/0

# Observability (optional)
LANGFUSE_HOST=your-langfuse-host
LANGFUSE_PUBLIC_KEY=your-public-key
LANGFUSE_SECRET_KEY=your-secret-key
```

## Key Architecture Patterns

### Tool System
All agent tools are centralized in `python-service/app/tools.py` and include:
- `knowledge_search`: Semantic search for service information
- `check_availability`: Check appointment slots
- `book_appointment`: Create appointments
- `cancel_appointment`: Cancel appointments
- `resolve_contact_on_booking`: Contact management

### State Management
Global state is managed through `GlobalState` TypedDict in `state.py` with fields for:
- Multi-tenancy (`organization_id`, `chat_identity_id`)
- Contact information (`contact_id`, `phone_number`, `country_code`)
- Appointment context (`service_id`, `selected_date`, `selected_time`, `available_slots`)
- Conversation flow (`current_flow`, `next_agent`)

### Media Processing
The Express Gateway handles media through `mediaProcessor.js`:
- **Audio**: Downloads → Supabase Storage → Gemini transcription → Text to agents
- **Images**: Downloads → Supabase Storage → Gemini description → Text to agents  
- **Video/Documents**: Downloads → Supabase Storage → Fallback message
- **Location/Contact**: Extracts data → Structured message (no file storage)

### Database Schema
Core tables in Supabase:
- `chat_messages`: Message history with `processed_text` for AI-processed content
- `appointments`: Booking data
- `contacts`: Customer information
- `platform_connections`: WhatsApp/Gupshup configuration

## Development Notes

- The system uses **Pydantic AI patterns** for structured LLM interactions
- **Multi-tenancy** is enforced at the organization level throughout
- **Media processing** converts audio/images to text before sending to AI agents
- **State persistence** uses LangGraph's checkpointing system
- **Error handling** includes graceful fallbacks and human escalation
- **Logging** is structured JSON for observability with Langfuse integration

## Testing

### API Endpoints
- Express Gateway: `http://localhost:8080/` (health check)
- Python Service: `http://localhost:8000/` (health check)
- Main agent endpoint: `POST http://localhost:8000/invoke`

### Webhook Testing
The system expects WhatsApp webhooks from Gupshup at `/webhooks/gupshup` with proper organization resolution middleware.