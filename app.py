#!/usr/bin/env python3
"""
Genesys Cloud Voicemail Web Exporter
====================================
Flask web application for self-service voicemail export.

Designed for PythonAnywhere deployment where users can export their own
voicemails via browser without installing any software.

Uses PKCE OAuth flow - each user logs in with their own Genesys credentials
and can only access their own voicemails.

Author: Filip Balakovski
"""

import os
import json
import hashlib
import base64
import secrets
import urllib.request
import urllib.parse
import tempfile
import shutil
import zipfile
import time
from datetime import datetime
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for, 
    session, flash, send_file, jsonify, Response
)

# ============================================================================
# FLASK APP CONFIGURATION
# ============================================================================

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', secrets.token_hex(32))

# Session configuration
app.config['SESSION_TYPE'] = 'filesystem'
app.config['PERMANENT_SESSION_LIFETIME'] = 3600  # 1 hour

# ============================================================================
# GENESYS CONFIGURATION
# ============================================================================

# OAuth Client ID - CREATE IN GENESYS ADMIN > INTEGRATIONS > OAUTH
# Grant Type: Code Authorization (with PKCE)
# Redirect URI: https://<username>.pythonanywhere.com/callback
# Scopes: voicemail, voicemail:readonly
CLIENT_ID = os.environ.get('GENESYS_CLIENT_ID', '')  # Set in PythonAnywhere environment

# For local development, you can hardcode or use a config file
if not CLIENT_ID:
    CLIENT_ID = ''  # <-- PUT YOUR CLIENT ID HERE FOR TESTING

# Redirect URI - UPDATE FOR YOUR PYTHONANYWHERE USERNAME
# For local development: http://127.0.0.1:5000/callback
# For production: https://<username>.pythonanywhere.com/callback
REDIRECT_URI = os.environ.get('REDIRECT_URI', 'http://127.0.0.1:5000/callback')

# Genesys Cloud Regions
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

# Temporary directory for downloads
TEMP_DIR = os.path.join(tempfile.gettempdir(), 'voicemail_exports')
os.makedirs(TEMP_DIR, exist_ok=True)

# ============================================================================
# PKCE HELPER FUNCTIONS
# ============================================================================

def generate_code_verifier(length=128):
    """Generate a cryptographically random code verifier for PKCE"""
    allowed_chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~'
    return ''.join(secrets.choice(allowed_chars) for _ in range(length))


def generate_code_challenge(code_verifier):
    """Generate code challenge from code verifier using S256 method"""
    code_hash = hashlib.sha256(code_verifier.encode('ascii')).digest()
    code_challenge = base64.urlsafe_b64encode(code_hash).decode('ascii')
    return code_challenge.rstrip('=')


def generate_state():
    """Generate random state parameter for CSRF protection"""
    return secrets.token_urlsafe(32)


# ============================================================================
# AUTHENTICATION HELPERS
# ============================================================================

def login_required(f):
    """Decorator to require login for routes"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'access_token' not in session:
            flash('Please log in to access this page.', 'warning')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function


def exchange_code_for_token(auth_code, region_host, code_verifier):
    """Exchange authorization code for access token"""
    token_url = f"https://login.{region_host}/oauth/token"
    
    token_data = {
        'grant_type': 'authorization_code',
        'client_id': CLIENT_ID,
        'code': auth_code,
        'code_verifier': code_verifier,
        'redirect_uri': REDIRECT_URI,
    }
    
    data = urllib.parse.urlencode(token_data).encode()
    req = urllib.request.Request(token_url, data=data, method='POST')
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')
    
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode())
            return result.get('access_token'), None
    except urllib.request.HTTPError as e:
        error_body = e.read().decode()
        return None, f"HTTP {e.code}: {error_body}"
    except Exception as e:
        return None, str(e)


# ============================================================================
# GENESYS API FUNCTIONS
# ============================================================================

def get_user_info(access_token, region_host):
    """Get current user information from Genesys"""
    url = f"https://api.{region_host}/api/v2/users/me"
    
    req = urllib.request.Request(url)
    req.add_header('Authorization', f'Bearer {access_token}')
    
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        app.logger.error(f"Error getting user info: {e}")
        return None


def get_voicemails(access_token, region_host):
    """Get all voicemails for the authenticated user"""
    all_messages = []
    page_number = 1
    page_size = 100
    
    while True:
        url = f"https://api.{region_host}/api/v2/voicemail/me/messages"
        params = {'pageSize': page_size, 'pageNumber': page_number}
        url_with_params = f"{url}?{urllib.parse.urlencode(params)}"
        
        req = urllib.request.Request(url_with_params)
        req.add_header('Authorization', f'Bearer {access_token}')
        
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                data = json.loads(response.read().decode())
                
                if 'entities' not in data or not data['entities']:
                    break
                
                all_messages.extend(data['entities'])
                
                if page_number >= data.get('pageCount', 1):
                    break
                
                page_number += 1
                
        except urllib.request.HTTPError as e:
            app.logger.error(f"HTTP Error getting voicemails: {e.code} - {e.reason}")
            break
        except Exception as e:
            app.logger.error(f"Error getting voicemails: {e}")
            break
    
    return all_messages


def download_voicemail_media(access_token, region_host, message_id):
    """Download voicemail media file and return bytes"""
    url = f"https://api.{region_host}/api/v2/voicemail/messages/{message_id}/media"
    params = {'formatId': 'WAV'}
    url_with_params = f"{url}?{urllib.parse.urlencode(params)}"
    
    req = urllib.request.Request(url_with_params)
    req.add_header('Authorization', f'Bearer {access_token}')
    
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            content_type = response.headers.get('Content-Type', '')
            
            if 'application/json' in content_type:
                # Got JSON with media URL
                data = json.loads(response.read().decode())
                if 'mediaFileUri' in data:
                    # Download from the URI
                    media_req = urllib.request.Request(data['mediaFileUri'])
                    with urllib.request.urlopen(media_req, timeout=120) as media_response:
                        return media_response.read(), None
                return None, "No media URI in response"
            else:
                # Got direct media
                return response.read(), None
                
    except urllib.request.HTTPError as e:
        if e.code == 403:
            return None, "Access denied - you may not own this voicemail"
        return None, f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return None, str(e)


def format_filename(voicemail):
    """Generate a safe filename for a voicemail"""
    msg_id = voicemail.get('id', 'unknown')
    caller_name = voicemail.get('callerName', 'Unknown')
    caller_address = voicemail.get('callerAddress', '')
    created_date = voicemail.get('createdDate', '')
    
    # Format date
    if created_date:
        try:
            dt = datetime.fromisoformat(created_date.replace('Z', '+00:00'))
            date_str = dt.strftime('%Y%m%d_%H%M%S')
        except:
            date_str = 'unknown_date'
    else:
        date_str = 'unknown_date'
    
    # Sanitize caller name
    safe_caller = ''.join(c if c.isalnum() or c in ' -_' else '_' for c in str(caller_name))[:30]
    
    return f"{date_str}_{safe_caller}_{msg_id[:8]}.wav"


def format_duration(seconds):
    """Format duration in seconds to human-readable string"""
    if not seconds:
        return "Unknown"
    
    minutes = int(seconds) // 60
    secs = int(seconds) % 60
    
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def format_datetime(date_string):
    """Format ISO datetime to human-readable string"""
    if not date_string:
        return "Unknown"
    
    try:
        dt = datetime.fromisoformat(date_string.replace('Z', '+00:00'))
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    except:
        return date_string


# ============================================================================
# CLEANUP FUNCTIONS
# ============================================================================

def cleanup_old_exports():
    """Clean up export files older than 1 hour"""
    try:
        now = time.time()
        for item in os.listdir(TEMP_DIR):
            item_path = os.path.join(TEMP_DIR, item)
            if os.path.isfile(item_path):
                if now - os.path.getmtime(item_path) > 3600:  # 1 hour
                    os.remove(item_path)
            elif os.path.isdir(item_path):
                if now - os.path.getmtime(item_path) > 3600:
                    shutil.rmtree(item_path, ignore_errors=True)
    except Exception as e:
        app.logger.error(f"Error cleaning up exports: {e}")


# ============================================================================
# FLASK ROUTES
# ============================================================================

@app.route('/')
def index():
    """Home page with region selection and login"""
    # Clean up old exports periodically
    cleanup_old_exports()
    
    # Check if already logged in
    if 'access_token' in session and 'user_info' in session:
        return redirect(url_for('dashboard'))
    
    return render_template('index.html', 
                         regions=REGIONS,
                         client_configured=bool(CLIENT_ID))


@app.route('/login', methods=['POST'])
def login():
    """Initiate OAuth login flow"""
    if not CLIENT_ID:
        flash('OAuth Client ID not configured. Please contact administrator.', 'danger')
        return redirect(url_for('index'))
    
    region_key = request.form.get('region')
    if region_key not in REGIONS:
        flash('Invalid region selected.', 'danger')
        return redirect(url_for('index'))
    
    region = REGIONS[region_key]
    
    # Generate PKCE codes
    code_verifier = generate_code_verifier()
    code_challenge = generate_code_challenge(code_verifier)
    state = generate_state()
    
    # Store in session for callback
    session['code_verifier'] = code_verifier
    session['oauth_state'] = state
    session['region_key'] = region_key
    session['region_host'] = region['host']
    
    # Build authorization URL
    auth_params = {
        'client_id': CLIENT_ID,
        'response_type': 'code',
        'redirect_uri': REDIRECT_URI,
        'code_challenge': code_challenge,
        'code_challenge_method': 'S256',
        'state': state,
    }
    
    auth_url = f"https://login.{region['host']}/oauth/authorize?{urllib.parse.urlencode(auth_params)}"
    
    return redirect(auth_url)


@app.route('/callback')
def callback():
    """OAuth callback handler"""
    # Check for errors
    error = request.args.get('error')
    if error:
        error_description = request.args.get('error_description', error)
        flash(f'Login failed: {error_description}', 'danger')
        return redirect(url_for('index'))
    
    # Verify state parameter
    state = request.args.get('state')
    if state != session.get('oauth_state'):
        flash('Invalid state parameter. Please try again.', 'danger')
        return redirect(url_for('index'))
    
    # Get authorization code
    auth_code = request.args.get('code')
    if not auth_code:
        flash('No authorization code received.', 'danger')
        return redirect(url_for('index'))
    
    # Exchange code for token
    code_verifier = session.get('code_verifier')
    region_host = session.get('region_host')
    
    if not code_verifier or not region_host:
        flash('Session expired. Please try again.', 'danger')
        return redirect(url_for('index'))
    
    access_token, error = exchange_code_for_token(auth_code, region_host, code_verifier)
    
    if error:
        flash(f'Failed to get access token: {error}', 'danger')
        return redirect(url_for('index'))
    
    # Store token in session
    session['access_token'] = access_token
    
    # Get user info
    user_info = get_user_info(access_token, region_host)
    if user_info:
        session['user_info'] = user_info
        flash(f'Welcome, {user_info.get("name", "User")}!', 'success')
    else:
        flash('Logged in, but could not retrieve user info.', 'warning')
    
    # Clean up OAuth session data
    session.pop('code_verifier', None)
    session.pop('oauth_state', None)
    
    return redirect(url_for('dashboard'))


@app.route('/dashboard')
@login_required
def dashboard():
    """Main dashboard showing voicemails"""
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    region_key = session.get('region_key')
    user_info = session.get('user_info', {})
    
    # Fetch voicemails
    voicemails = get_voicemails(access_token, region_host)
    
    # Process voicemails for display
    processed_voicemails = []
    for vm in voicemails:
        processed_voicemails.append({
            'id': vm.get('id'),
            'caller_name': vm.get('callerName', 'Unknown'),
            'caller_address': vm.get('callerAddress', ''),
            'created_date': format_datetime(vm.get('createdDate')),
            'created_date_raw': vm.get('createdDate', ''),
            'duration': format_duration(vm.get('audioRecordingDurationSeconds')),
            'duration_seconds': vm.get('audioRecordingDurationSeconds', 0),
            'read': vm.get('read', False),
            'filename': format_filename(vm),
        })
    
    return render_template('dashboard.html',
                         user_info=user_info,
                         region=REGIONS.get(region_key, {}),
                         voicemails=processed_voicemails,
                         voicemail_count=len(processed_voicemails))


@app.route('/download/<message_id>')
@login_required
def download_single(message_id):
    """Download a single voicemail"""
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    
    # Get voicemail list to find the filename
    voicemails = get_voicemails(access_token, region_host)
    voicemail = next((vm for vm in voicemails if vm.get('id') == message_id), None)
    
    if not voicemail:
        flash('Voicemail not found.', 'danger')
        return redirect(url_for('dashboard'))
    
    # Download the media
    media_bytes, error = download_voicemail_media(access_token, region_host, message_id)
    
    if error:
        flash(f'Failed to download voicemail: {error}', 'danger')
        return redirect(url_for('dashboard'))
    
    filename = format_filename(voicemail)
    
    return Response(
        media_bytes,
        mimetype='audio/wav',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )


@app.route('/download-all')
@login_required
def download_all():
    """Download all voicemails as a ZIP file"""
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    user_info = session.get('user_info', {})
    
    # Get voicemails
    voicemails = get_voicemails(access_token, region_host)
    
    if not voicemails:
        flash('No voicemails to download.', 'warning')
        return redirect(url_for('dashboard'))
    
    # Create temporary directory for this export
    user_name = user_info.get('name', 'Unknown').replace(' ', '_')
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    export_name = f"voicemails_{user_name}_{timestamp}"
    export_dir = os.path.join(TEMP_DIR, export_name)
    os.makedirs(export_dir, exist_ok=True)
    
    downloaded = 0
    errors = []
    
    # Download each voicemail
    for vm in voicemails:
        msg_id = vm.get('id')
        filename = format_filename(vm)
        
        media_bytes, error = download_voicemail_media(access_token, region_host, msg_id)
        
        if error:
            errors.append(f"{filename}: {error}")
            continue
        
        # Save to file
        filepath = os.path.join(export_dir, filename)
        with open(filepath, 'wb') as f:
            f.write(media_bytes)
        downloaded += 1
    
    # Save metadata
    metadata_file = os.path.join(export_dir, 'metadata.json')
    with open(metadata_file, 'w', encoding='utf-8') as f:
        json.dump({
            'exported_by': user_info.get('name'),
            'exported_at': datetime.now().isoformat(),
            'total_voicemails': len(voicemails),
            'downloaded': downloaded,
            'errors': errors,
            'voicemails': voicemails
        }, f, indent=2, default=str)
    
    # Create ZIP file
    zip_path = os.path.join(TEMP_DIR, f"{export_name}.zip")
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(export_dir):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, export_dir)
                zipf.write(file_path, arcname)
    
    # Clean up directory
    shutil.rmtree(export_dir, ignore_errors=True)
    
    # Send the ZIP file
    if errors:
        flash(f'Downloaded {downloaded}/{len(voicemails)} voicemails. Some failed.', 'warning')
    
    return send_file(
        zip_path,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f"{export_name}.zip"
    )


@app.route('/api/voicemails')
@login_required
def api_voicemails():
    """API endpoint to get voicemails as JSON"""
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    
    voicemails = get_voicemails(access_token, region_host)
    
    processed = []
    for vm in voicemails:
        processed.append({
            'id': vm.get('id'),
            'caller_name': vm.get('callerName', 'Unknown'),
            'caller_address': vm.get('callerAddress', ''),
            'created_date': vm.get('createdDate'),
            'duration_seconds': vm.get('audioRecordingDurationSeconds', 0),
            'read': vm.get('read', False),
        })
    
    return jsonify({'voicemails': processed, 'count': len(processed)})


@app.route('/logout')
def logout():
    """Log out and clear session"""
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('index'))


@app.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'client_configured': bool(CLIENT_ID),
        'timestamp': datetime.now().isoformat()
    })


# ============================================================================
# ERROR HANDLERS
# ============================================================================

@app.errorhandler(404)
def not_found(e):
    return render_template('error.html', 
                         error_code=404, 
                         error_message='Page not found'), 404


@app.errorhandler(500)
def server_error(e):
    return render_template('error.html', 
                         error_code=500, 
                         error_message='Internal server error'), 500


# ============================================================================
# TEMPLATE FILTERS
# ============================================================================

@app.template_filter('datetime')
def datetime_filter(value):
    return format_datetime(value)


@app.template_filter('duration')
def duration_filter(value):
    return format_duration(value)


# ============================================================================
# MAIN
# ============================================================================

if __name__ == '__main__':
    # Local development
    app.run(debug=True, host='127.0.0.1', port=5000)
