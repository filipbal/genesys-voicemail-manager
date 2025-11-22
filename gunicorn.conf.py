# Gunicorn configuration for Voicemail Manager
# This config increases timeouts for long-running batch operations

# Worker timeout - set high for batch operations
# Default is 30 seconds, we need much more for 500+ voicemail operations
timeout = 600  # 10 minutes

# Graceful timeout for worker restart
graceful_timeout = 120

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
