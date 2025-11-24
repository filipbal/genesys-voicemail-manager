# Gunicorn configuration for Voicemail Manager
# Updated for large downloads (500+ voicemails)

# Worker timeout - set to 5400s (90 minutes) for safety margin
timeout = 5400  # 90 minutes (leaves 10min buffer under Render's 100min limit)

# Graceful timeout for worker restart
graceful_timeout = 300  # 5 minutes (increased for long operations)

# Keep-alive connections
keepalive = 2  # Lower = less overhead, we're doing long-running operations

# Number of workers
workers = 2  # Keep at 2

# Worker class - sync is fine for our use case
worker_class = "sync"

# Logging
accesslog = "-"
errorlog = "-"
loglevel = "info"

# Bind to port (Render sets PORT env var)
import os
bind = f"0.0.0.0:{os.environ.get('PORT', '5000')}"