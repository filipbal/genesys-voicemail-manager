# Genesys Voicemail Exporter - Configuration
# Copy this file to config.py and update the values

# =============================================================================
# REQUIRED CONFIGURATION
# =============================================================================

# OAuth Client ID from Genesys Admin > Integrations > OAuth
# Grant Type: Code Authorization (with PKCE)
GENESYS_CLIENT_ID = "92e147a9-91a2-4d15-aa14-7100bbb6b7fe"

# Redirect URI - must match exactly in Genesys OAuth client settings
# For PythonAnywhere: https://<username>.pythonanywhere.com/callback
# For local development: http://127.0.0.1:5000/callback
REDIRECT_URI = "https://filipbalakovskintt.eu.pythonanywhere.com/callback"

# Flask secret key for session encryption
# Generate with: python -c "import secrets; print(secrets.token_hex(32))"
FLASK_SECRET_KEY = "0e943ed4bf5c378a0d55d1549536bded9619a6aa3ddf8c6fce8a11e0daaa706b"

# =============================================================================
# OPTIONAL CONFIGURATION
# =============================================================================

# Session lifetime in seconds (default: 1 hour)
SESSION_LIFETIME = 3600

# Temporary file cleanup interval in seconds (default: 1 hour)
CLEANUP_INTERVAL = 3600

# =============================================================================
# GENESYS REGIONS (DO NOT MODIFY)
# =============================================================================

REGIONS = {
    "emea": {"name": "EMEA (Frankfurt)", "host": "mypurecloud.de"},
    "us_west": {"name": "US West", "host": "usw2.pure.cloud"},
    "us_east": {"name": "US East", "host": "mypurecloud.com"},
    "asia_mumbai": {"name": "Asia Pacific South (Mumbai)", "host": "aps1.pure.cloud"},
    "asia_tokyo": {"name": "Asia Pacific (Tokyo)", "host": "mypurecloud.jp"},
    "asia_sydney": {"name": "Asia Pacific (Sydney)", "host": "mypurecloud.com.au"},
    "canada": {"name": "Canada", "host": "cac1.pure.cloud"},
    "eu_london": {"name": "Europe (London)", "host": "euw2.pure.cloud"},
    "eu_ireland": {"name": "Europe (Ireland)", "host": "mypurecloud.ie"},
}
