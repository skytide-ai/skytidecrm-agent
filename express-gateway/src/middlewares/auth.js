const { createClient } = require('@supabase/supabase-js');

// Configuración de Supabase
const supabaseUrl = process.env.SUPABASE_URL;
const supabaseAnonKey = process.env.SUPABASE_ANON_KEY;
const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY;

if (!supabaseUrl || !supabaseAnonKey || !supabaseServiceKey) {
    console.error('❌ Error: SUPABASE_URL, SUPABASE_ANON_KEY y SUPABASE_SERVICE_ROLE_KEY deben estar configuradas en las variables de entorno');
    process.exit(1);
}

// Cliente con anon key para consultas de lectura básicas
const supabase = createClient(supabaseUrl, supabaseAnonKey);

// Cliente con service key para operaciones de webhook (insert/update privilegiadas)
const supabaseService = createClient(supabaseUrl, supabaseServiceKey);

const resolveOrganization = async (req, res, next) => {
    try {
        // El payload de Gupshup puede variar, asumimos que el appName viene en req.body.app
        const appName = req.body.app; 

        if (!appName) {
            console.error('Gupshup app name not found in request body');
            return res.status(400).json({ error: 'Gupshup app name not found in request body' });
        }

        console.log(`Buscando organización para app: ${appName}`);

        // Consultar la tabla platform_connections en Supabase incluyendo credenciales de Gupshup
        const { data: connection, error } = await supabase
            .from('platform_connections')
            .select('organization_id, is_active, gupshup_api_key, gupshup_app_name, whatsapp_business_number')
            .eq('gupshup_app_name', appName)
            .eq('is_active', true)
            .eq('platform', 'whatsapp')
            .single();

        if (error) {
            console.error('Error al consultar platform_connections:', error);
            if (error.code === 'PGRST116') {
                // No rows returned
                console.error(`Organization not found or inactive for app: ${appName}`);
                return res.status(404).json({ 
                    error: `Organization not found or inactive for app: ${appName}` 
                });
            } else {
                // Other database errors
                console.error('Database error:', error.message);
                return res.status(500).json({ 
                    error: 'Internal server error while resolving organization' 
                });
            }
        }

        if (!connection) {
            console.error(`Organization not found or inactive for app: ${appName}`);
            return res.status(404).json({ 
                error: `Organization not found or inactive for app: ${appName}` 
            });
        }

        // Añadimos el organization_id y las credenciales de Gupshup al objeto request para uso posterior
        req.organizationId = connection.organization_id;
        req.gupshupApiKey = connection.gupshup_api_key;
        req.gupshupAppName = connection.gupshup_app_name;
        req.whatsappBusinessNumber = connection.whatsapp_business_number;
        
        console.log(`✅ Request resolved to organizationId: ${req.organizationId} for app: ${appName}`);
        console.log(`✅ Gupshup credentials loaded for organization`);

        // Validar que tenemos las credenciales necesarias
        if (!req.gupshupApiKey || !req.whatsappBusinessNumber) {
            console.error('Gupshup credentials incomplete for organization:', req.organizationId);
            return res.status(500).json({ 
                error: 'Gupshup configuration incomplete for this organization' 
            });
        }

        // Pasamos el control al siguiente middleware o a la ruta
        next();

    } catch (error) {
        console.error('Unexpected error in resolveOrganization middleware:', error);
        return res.status(500).json({ 
            error: 'Internal server error while resolving organization' 
        });
    }
};

// Nueva función para resolver chat_identity
const resolveChatIdentity = async (req, res, next) => {
  try {
    // Necesitamos que organization_id ya esté resuelto
    if (!req.organizationId) {
      return res.status(400).json({ error: 'Organization ID not resolved' });
    }

    // Extraer datos del remitente desde Gupshup
    const sender = req.body?.payload?.sender;
    const phone = sender?.phone;           // Número completo (platform_user_id)
    const countryCode = sender?.country_code; // Código de país (sin +)
    const dialCode = sender?.dial_code;    // Número nacional

    if (!phone || !countryCode || !dialCode) {
      console.error('Sender information incomplete in Gupshup payload');
      return res.status(400).json({ error: 'Sender phone information not found in payload' });
    }

    console.log(`Resolving chat identity for organization: ${req.organizationId}, phone: ${phone}`);

    // 1. Buscar chat_identity existente (lectura con service key para evitar RLS)
    const { data: existingChatIdentity, error: searchError } = await supabaseService
      .from('chat_identities')
      .select('id, contact_id')
      .eq('organization_id', req.organizationId)
      .eq('platform_user_id', phone)
      .eq('platform', 'whatsapp')
      .maybeSingle();

    if (searchError) {
      console.error('Error searching chat_identity:', searchError);
      return res.status(500).json({ error: 'Database error while searching chat identity' });
    }

    let chatIdentityId;
    let contactId = null;

    if (existingChatIdentity) {
      // Chat identity ya existe
      chatIdentityId = existingChatIdentity.id;
      contactId = existingChatIdentity.contact_id;
      console.log(`Existing chat identity found: ${chatIdentityId}, contact_id: ${contactId}`);
      
      // Actualizar last_seen (escritura con service key)
      await supabaseService
        .from('chat_identities')
        .update({ last_seen: new Date().toISOString() })
        .eq('id', chatIdentityId);
        
    } else {
      // Crear nueva chat_identity (escritura con service key)
      console.log('Creating new chat identity...');
      const { data: newChatIdentity, error: insertError } = await supabaseService
        .from('chat_identities')
        .insert({
          organization_id: req.organizationId,
          platform_user_id: phone,
          platform: 'whatsapp'
        })
        .select('id')
        .single();

      if (insertError || !newChatIdentity) {
        console.error('Error creating chat_identity:', insertError);
        return res.status(500).json({ error: 'Database error while creating chat identity' });
      }

      chatIdentityId = newChatIdentity.id;
      console.log(`New chat identity created: ${chatIdentityId}`);
    }

    // Agregar datos resueltos al request
    req.chatIdentityId = chatIdentityId;
    req.contactId = contactId;
    req.phone = phone;
    req.countryCode = `+${countryCode}`;
    req.phoneNumber = dialCode;

    console.log(`✅ Chat identity resolved: ${chatIdentityId}`);
    next();

  } catch (error) {
    console.error('Unexpected error in resolveChatIdentity:', error);
    res.status(500).json({ error: 'Internal server error while resolving chat identity' });
  }
};

module.exports = { resolveOrganization, resolveChatIdentity }; 