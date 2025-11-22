"""
WSGI configuration for PythonAnywhere deployment.

Instructions:
1. Upload all files to PythonAnywhere
2. Go to Web tab and create a new web app
3. Select "Manual configuration" -> Python 3.10+
4. Set the source code directory to your app folder
5. Edit the WSGI configuration file and replace with this content
6. Set environment variables in the Web tab:
   - GENESYS_CLIENT_ID: Your OAuth Client ID
   - REDIRECT_URI: https://<username>.pythonanywhere.com/callback
   - FLASK_SECRET_KEY: A random secret string
"""

import sys
import os

# Add your project directory to the path
# IMPORTANT: Update this path to match your PythonAnywhere username
project_home = '/home/<YOUR_USERNAME>/voicemail_exporter'
if project_home not in sys.path:
    sys.path.insert(0, project_home)

# Set environment variables (if not set in PythonAnywhere Web tab)
# Uncomment and set these if needed:
# os.environ['GENESYS_CLIENT_ID'] = 'your-client-id-here'
# os.environ['REDIRECT_URI'] = 'https://<username>.pythonanywhere.com/callback'
# os.environ['FLASK_SECRET_KEY'] = 'your-secret-key-here'

from app import app as application
