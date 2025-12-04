# gunicorn.conf.py
import os

# Worker timeout - 90 minutes
timeout = 5400

# Graceful timeout
graceful_timeout = 300

# Keep-alive - increase for Render's load balancer
keepalive = 75

# Workers - 2 is fine for single user + 0.1 CPU
workers = 2

# Worker class
worker_class = "sync"

# Logging
accesslog = "-"
errorlog = "-"
loglevel = "info"

# Bind
bind = f"0.0.0.0:{os.environ.get('PORT', '5000')}"