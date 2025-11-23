# Gunicorn configuration for Voicemail Manager
# Updated for large downloads (500+ voicemails)

# Worker timeout - increased to 900s (15 minutes) for large downloads
timeout = 900  # 15 minutes (was 600)

# Graceful timeout for worker restart
graceful_timeout = 150

# Keep-alive connections
keepalive = 5

# Number of workers
workers = 2

# Worker class - sync is fine for our use case
worker_class = "sync"

# Logging
accesslog = "-"
errorlog = "-"
loglevel = "info"

# Bind to port (Render sets PORT env var)
import os
bind = f"0.0.0.0:{os.environ.get('PORT', '5000')}"