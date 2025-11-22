#!/usr/bin/env python3
"""
Genesys Cloud Voicemail Web Exporter
====================================
Flask web application for self-service voicemail export and forwarding.

Designed for Render.com deployment where users can export their own
voicemails via browser without installing any software.

Uses PKCE OAuth flow - each user logs in with their own Genesys credentials
and can only access their own voicemails.

Features:
- Download individual voicemails or all as ZIP
- Forward voicemails to other users or groups
- Batch processing with rate limit handling

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
# Redirect URI: https://your-app.onrender.com/callback
# Scopes: voicemail, voicemail:readonly, users:readonly, groups:readonly
CLIENT_ID = os.environ.get('GENESYS_CLIENT_ID', '')

# For local development, you can hardcode or use a config file
if not CLIENT_ID:
    CLIENT_ID = ''  # <-- PUT YOUR CLIENT ID HERE FOR TESTING

# Redirect URI - UPDATE FOR YOUR DEPLOYMENT
REDIRECT_URI = os.environ.get('REDIRECT_URI', 'http://127.0.0.1:5000/callback')

# Genesys Cloud Regions
REGIONS = {
    "us_west": {"name": "US West", "host": "usw2.pure.cloud"}
}

# Temporary directory for downloads
TEMP_DIR = os.path.join(tempfile.gettempdir(), 'voicemail_exports')
os.makedirs(TEMP_DIR, exist_ok=True)

# ============================================================================
# RATE LIMITING AND BATCH CONFIGURATION
# ============================================================================

# Genesys API limits - conservative settings to avoid 429 errors
API_PAGE_SIZE = 100  # Genesys API max is typically 100
DISPLAY_PAGE_SIZE = 50  # Number of voicemails to display per page in UI

# Batch processing settings
BATCH_SIZE = 25  # Number of operations per batch (forward/delete)
BATCH_DELAY = 2.0  # Seconds to wait between batches
OPERATION_DELAY = 0.3  # Seconds between individual operations within a batch
RATE_LIMIT_BACKOFF = 5.0  # Seconds to wait after rate limit hit
MAX_RETRIES = 3  # Max retries for rate-limited requests

# Cache settings (in-memory, per session)
CACHE_TTL = 60  # Seconds to cache voicemail data


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
# RATE-LIMITED API REQUEST HELPER
# ============================================================================

def make_api_request(url, access_token, method='GET', data=None, retries=MAX_RETRIES):
    """
    Make an API request with automatic retry on rate limit (429).
    
    Args:
        url: Full URL to request
        access_token: Bearer token
        method: HTTP method (GET, POST, DELETE, PATCH)
        data: Request body for POST/PATCH (will be JSON encoded)
        retries: Number of retries remaining
    
    Returns:
        (response_data, error_message)
    """
    req = urllib.request.Request(url, method=method)
    req.add_header('Authorization', f'Bearer {access_token}')
    
    if data is not None:
        json_data = json.dumps(data).encode()
        req.data = json_data
        req.add_header('Content-Type', 'application/json')
    
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            if response.status == 204:  # No content (DELETE success)
                return None, None
            return json.loads(response.read().decode()), None
            
    except urllib.request.HTTPError as e:
        if e.code == 429 and retries > 0:
            # Rate limited - back off and retry
            retry_after = e.headers.get('Retry-After', RATE_LIMIT_BACKOFF)
            try:
                wait_time = float(retry_after)
            except:
                wait_time = RATE_LIMIT_BACKOFF
            
            app.logger.warning(f"Rate limited (429). Waiting {wait_time}s before retry. Retries left: {retries-1}")
            time.sleep(wait_time)
            return make_api_request(url, access_token, method, data, retries - 1)
        
        try:
            error_body = json.loads(e.read().decode())
            error_msg = error_body.get('message', str(error_body))
        except:
            error_msg = f"HTTP {e.code}: {e.reason}"
        
        return None, error_msg
        
    except Exception as e:
        return None, str(e)


# ============================================================================
# GENESYS API FUNCTIONS
# ============================================================================

def get_user_info(access_token, region_host):
    """Get current user information from Genesys"""
    url = f"https://api.{region_host}/api/v2/users/me"
    data, error = make_api_request(url, access_token)
    
    if error:
        app.logger.error(f"Error getting user info: {error}")
        return None
    return data


def is_voicemail_deleted(vm):
    """Check if a voicemail is marked as deleted"""
    if vm.get('deleted', False):
        return True
    if vm.get('state', '').lower() == 'deleted':
        return True
    if vm.get('deletedDate') is not None:
        return True
    return False


def get_voicemails_page(access_token, region_host, page_number=1, page_size=API_PAGE_SIZE):
    """
    Get a single page of voicemails from the API.
    Returns: (voicemails_list, total_from_api, page_count, has_more, error)
    
    Note: total_from_api includes deleted items; filtering happens client-side
    """
    url = f"https://api.{region_host}/api/v2/voicemail/messages"
    params = {'pageSize': page_size, 'pageNumber': page_number}
    url_with_params = f"{url}?{urllib.parse.urlencode(params)}"
    
    data, error = make_api_request(url_with_params, access_token)
    
    if error:
        app.logger.error(f"Error getting voicemails page {page_number}: {error}")
        return [], 0, 0, False, error
    
    entities = data.get('entities', [])
    total = data.get('total', 0)
    page_count = data.get('pageCount', 1)
    
    # Filter out deleted voicemails
    active_voicemails = [vm for vm in entities if not is_voicemail_deleted(vm)]
    
    has_more = page_number < page_count
    
    return active_voicemails, total, page_count, has_more, None


def get_all_voicemails_paginated(access_token, region_host, max_pages=None, progress_callback=None):
    """
    Get ALL voicemails across all pages (excluding deleted).
    
    Args:
        max_pages: Optional limit on number of pages to fetch (None = all pages)
        progress_callback: Optional callback(current_page, total_pages) for progress
    
    Returns: (list of all voicemails, total_active_count, error)
    """
    all_messages = []
    page_number = 1
    api_page_count = None
    
    while True:
        voicemails, total, page_count, has_more, error = get_voicemails_page(
            access_token, region_host, page_number, API_PAGE_SIZE
        )
        
        if error:
            return all_messages, len(all_messages), error
        
        if api_page_count is None:
            api_page_count = page_count
        
        all_messages.extend(voicemails)
        
        if progress_callback:
            progress_callback(page_number, api_page_count)
        
        if not has_more:
            break
            
        if max_pages and page_number >= max_pages:
            break
            
        page_number += 1
        
        # Small delay between pages to avoid rate limiting
        time.sleep(0.1)
    
    return all_messages, len(all_messages), None


def get_voicemail_count_fast(access_token, region_host):
    """
    Quick count of voicemails by fetching just the first page.
    Returns API total (may include recently deleted) and page count.
    For accurate count, use get_all_voicemails_paginated.
    """
    url = f"https://api.{region_host}/api/v2/voicemail/messages"
    params = {'pageSize': 1, 'pageNumber': 1}
    url_with_params = f"{url}?{urllib.parse.urlencode(params)}"
    
    data, error = make_api_request(url_with_params, access_token)
    
    if error:
        return 0, 0
    
    return data.get('total', 0), data.get('pageCount', 0)


def get_voicemail_stats_cached(access_token, region_host, force_refresh=False):
    """
    Get voicemail statistics with session caching.
    Caches results to avoid repeated full scans.
    """
    cache_key = 'voicemail_stats'
    cache_time_key = 'voicemail_stats_time'
    
    # Check cache
    if not force_refresh:
        cached_stats = session.get(cache_key)
        cached_time = session.get(cache_time_key, 0)
        
        if cached_stats and (time.time() - cached_time) < CACHE_TTL:
            return cached_stats
    
    # Fetch fresh data
    all_voicemails, total_count, error = get_all_voicemails_paginated(access_token, region_host)
    
    if error:
        app.logger.error(f"Error getting voicemail stats: {error}")
        # Return cached if available, otherwise zeros
        return session.get(cache_key, {
            'total_count': 0,
            'total_duration': 0,
            'unread_count': 0
        })
    
    stats = {
        'total_count': total_count,
        'total_duration': sum(vm.get('audioRecordingDurationSeconds', 0) or 0 for vm in all_voicemails),
        'unread_count': sum(1 for vm in all_voicemails if not vm.get('read', True))
    }
    
    # Cache results
    session[cache_key] = stats
    session[cache_time_key] = time.time()
    
    return stats


def invalidate_voicemail_cache():
    """Invalidate the voicemail cache after modifications"""
    session.pop('voicemail_stats', None)
    session.pop('voicemail_stats_time', None)
    session.pop('voicemail_list_cache', None)
    session.pop('voicemail_list_cache_time', None)


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


def search_users(access_token, region_host, query):
    """Search for users by name or email"""
    url = f"https://api.{region_host}/api/v2/users/search"
    
    search_body = {
        "pageSize": 25,
        "pageNumber": 1,
        "query": [
            {
                "type": "QUERY_STRING",
                "fields": ["name", "email"],
                "value": f"*{query}*"
            }
        ]
    }
    
    data, error = make_api_request(url, access_token, method='POST', data=search_body)
    
    if error:
        return None, error
    
    return data.get('results', []), None


def search_groups(access_token, region_host, query):
    """Search for groups by name using POST search endpoint"""
    url = f"https://api.{region_host}/api/v2/groups/search"
    
    search_body = {
        "pageSize": 25,
        "pageNumber": 1,
        "query": [
            {
                "type": "STARTS_WITH",
                "fields": ["name"],
                "value": query
            }
        ]
    }
    
    data, error = make_api_request(url, access_token, method='POST', data=search_body)
    
    if error:
        return None, error
    
    return data.get('results', []), None


# ============================================================================
# BATCH OPERATIONS WITH RATE LIMIT HANDLING
# ============================================================================

def forward_voicemail_single(access_token, region_host, voicemail_id, target_id, target_type='user'):
    """Forward a single voicemail to a user or group"""
    url = f"https://api.{region_host}/api/v2/voicemail/messages"
    
    if target_type == 'group':
        copy_body = {
            "groupId": target_id,
            "voicemailMessageId": voicemail_id
        }
    else:
        copy_body = {
            "userId": target_id,
            "voicemailMessageId": voicemail_id
        }
    
    data, error = make_api_request(url, access_token, method='POST', data=copy_body)
    
    if error:
        return False, error
    
    return True, data


def delete_voicemail_single(access_token, region_host, voicemail_id):
    """Delete a single voicemail message"""
    url = f"https://api.{region_host}/api/v2/voicemail/messages/{voicemail_id}"
    
    data, error = make_api_request(url, access_token, method='DELETE')
    
    if error:
        return False, error
    
    return True, "Deleted successfully"


def process_voicemails_in_batches(access_token, region_host, voicemail_ids, operation, 
                                   target_id=None, target_type='user'):
    """
    Process voicemails in batches to avoid rate limiting.
    
    Args:
        voicemail_ids: List of voicemail IDs to process
        operation: 'forward' or 'delete'
        target_id: For forward operation, the target user/group ID
        target_type: For forward operation, 'user' or 'group'
    
    Returns:
        dict with 'success', 'failed', 'errors', 'processed' counts
    """
    results = {
        'success': 0,
        'failed': 0,
        'errors': [],
        'total': len(voicemail_ids),
        'processed': 0
    }
    
    total_ids = len(voicemail_ids)
    
    # Process in batches
    for batch_start in range(0, total_ids, BATCH_SIZE):
        batch_end = min(batch_start + BATCH_SIZE, total_ids)
        batch = voicemail_ids[batch_start:batch_end]
        batch_num = (batch_start // BATCH_SIZE) + 1
        total_batches = (total_ids + BATCH_SIZE - 1) // BATCH_SIZE
        
        app.logger.info(f"Processing batch {batch_num}/{total_batches} ({len(batch)} items)")
        
        for idx, vm_id in enumerate(batch):
            try:
                if operation == 'forward':
                    success, result = forward_voicemail_single(
                        access_token, region_host, vm_id, target_id, target_type
                    )
                elif operation == 'delete':
                    success, result = delete_voicemail_single(access_token, region_host, vm_id)
                else:
                    success, result = False, f"Unknown operation: {operation}"
                
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
                    results['errors'].append(f"VM {vm_id[:8]}...: {result}")
                
                results['processed'] += 1
                
                # Delay between operations within batch
                if idx < len(batch) - 1:
                    time.sleep(OPERATION_DELAY)
                    
            except Exception as e:
                results['failed'] += 1
                results['errors'].append(f"VM {vm_id[:8]}...: {str(e)}")
                results['processed'] += 1
        
        # Longer delay between batches
        if batch_end < total_ids:
            app.logger.info(f"Batch {batch_num} complete. Waiting {BATCH_DELAY}s before next batch...")
            time.sleep(BATCH_DELAY)
    
    # Invalidate cache after modifications
    invalidate_voicemail_cache()
    
    return results


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def format_voicemail(vm):
    """Format a voicemail object for display"""
    return {
        'id': vm.get('id'),
        'caller_name': vm.get('callerName', 'Unknown'),
        'caller_address': vm.get('callerAddress', ''),
        'created_date': format_datetime(vm.get('createdDate')),
        'created_date_raw': vm.get('createdDate', ''),
        'duration': format_duration(vm.get('audioRecordingDurationSeconds')),
        'duration_seconds': vm.get('audioRecordingDurationSeconds', 0) or 0,
        'read': vm.get('read', False),
        'filename': format_filename(vm),
    }


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
    cleanup_old_exports()
    
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
    
    code_verifier = generate_code_verifier()
    code_challenge = generate_code_challenge(code_verifier)
    state = generate_state()
    
    session['code_verifier'] = code_verifier
    session['oauth_state'] = state
    session['region_key'] = region_key
    session['region_host'] = region['host']
    
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
    error = request.args.get('error')
    if error:
        error_description = request.args.get('error_description', error)
        flash(f'Login failed: {error_description}', 'danger')
        return redirect(url_for('index'))
    
    state = request.args.get('state')
    if state != session.get('oauth_state'):
        flash('Invalid state parameter. Please try again.', 'danger')
        return redirect(url_for('index'))
    
    auth_code = request.args.get('code')
    if not auth_code:
        flash('No authorization code received.', 'danger')
        return redirect(url_for('index'))
    
    code_verifier = session.get('code_verifier')
    region_host = session.get('region_host')
    
    if not code_verifier or not region_host:
        flash('Session expired. Please try again.', 'danger')
        return redirect(url_for('index'))
    
    access_token, error = exchange_code_for_token(auth_code, region_host, code_verifier)
    
    if error:
        flash(f'Failed to get access token: {error}', 'danger')
        return redirect(url_for('index'))
    
    session['access_token'] = access_token
    
    user_info = get_user_info(access_token, region_host)
    if user_info:
        session['user_info'] = user_info
        flash(f'Welcome, {user_info.get("name", "User")}!', 'success')
    else:
        flash('Logged in, but could not retrieve user info.', 'warning')
    
    session.pop('code_verifier', None)
    session.pop('oauth_state', None)
    
    # Clear any stale cache
    invalidate_voicemail_cache()
    
    return redirect(url_for('dashboard'))


@app.route('/dashboard')
@login_required
def dashboard():
    """Main dashboard showing voicemails with pagination"""
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    region_key = session.get('region_key')
    user_info = session.get('user_info', {})
    
    # Get page number from query string (default to 1)
    page = request.args.get('page', 1, type=int)
    force_refresh = request.args.get('refresh', '0') == '1'
    
    if page < 1:
        page = 1
    
    if force_refresh:
        invalidate_voicemail_cache()
    
    # Get all voicemails (with caching consideration)
    all_voicemails, total_count, error = get_all_voicemails_paginated(access_token, region_host)
    
    if error:
        flash(f'Error loading voicemails: {error}', 'warning')
        all_voicemails = []
        total_count = 0
    
    # Calculate pagination
    total_pages = (total_count + DISPLAY_PAGE_SIZE - 1) // DISPLAY_PAGE_SIZE if total_count > 0 else 1
    
    if page > total_pages:
        page = total_pages
    
    # Get slice for current page
    start_idx = (page - 1) * DISPLAY_PAGE_SIZE
    end_idx = start_idx + DISPLAY_PAGE_SIZE
    page_voicemails = all_voicemails[start_idx:end_idx]
    
    # Format voicemails for display
    processed_voicemails = [format_voicemail(vm) for vm in page_voicemails]
    
    # Calculate stats from the full list (efficient since we already have it)
    total_duration = sum(vm.get('audioRecordingDurationSeconds', 0) or 0 for vm in all_voicemails)
    unread_count = sum(1 for vm in all_voicemails if not vm.get('read', True))
    total_duration_minutes = round(total_duration / 60, 1) if total_duration else 0
    
    return render_template('dashboard.html',
                         user_info=user_info,
                         region=REGIONS.get(region_key, {}),
                         voicemails=processed_voicemails,
                         voicemail_count=total_count,
                         total_duration_minutes=total_duration_minutes,
                         unread_count=unread_count,
                         # Pagination info
                         current_page=page,
                         total_pages=total_pages,
                         has_prev=page > 1,
                         has_next=page < total_pages,
                         page_size=DISPLAY_PAGE_SIZE,
                         start_idx=start_idx + 1 if total_count > 0 else 0,
                         end_idx=min(end_idx, total_count))


@app.route('/download/<message_id>')
@login_required
def download_single(message_id):
    """Download a single voicemail"""
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    
    # Get voicemail metadata (need to fetch to get filename info)
    all_voicemails, _, _ = get_all_voicemails_paginated(access_token, region_host)
    voicemail = next((vm for vm in all_voicemails if vm.get('id') == message_id), None)
    
    if not voicemail:
        flash('Voicemail not found.', 'danger')
        return redirect(url_for('dashboard'))
    
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
    
    all_voicemails, total_count, error = get_all_voicemails_paginated(access_token, region_host)
    
    if error or not all_voicemails:
        flash('No voicemails to download.', 'warning')
        return redirect(url_for('dashboard'))
    
    user_name = user_info.get('name', 'Unknown').replace(' ', '_')
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    export_name = f"voicemails_{user_name}_{timestamp}"
    export_dir = os.path.join(TEMP_DIR, export_name)
    os.makedirs(export_dir, exist_ok=True)
    
    downloaded = 0
    errors = []
    
    for vm in all_voicemails:
        msg_id = vm.get('id')
        filename = format_filename(vm)
        
        media_bytes, error = download_voicemail_media(access_token, region_host, msg_id)
        
        if error:
            errors.append(f"{filename}: {error}")
            continue
        
        filepath = os.path.join(export_dir, filename)
        with open(filepath, 'wb') as f:
            f.write(media_bytes)
        downloaded += 1
        
        # Small delay to avoid rate limiting
        time.sleep(0.2)
    
    metadata_file = os.path.join(export_dir, 'metadata.json')
    with open(metadata_file, 'w', encoding='utf-8') as f:
        json.dump({
            'exported_by': user_info.get('name'),
            'exported_at': datetime.now().isoformat(),
            'total_voicemails': len(all_voicemails),
            'downloaded': downloaded,
            'errors': errors,
            'voicemails': all_voicemails
        }, f, indent=2, default=str)
    
    zip_path = os.path.join(TEMP_DIR, f"{export_name}.zip")
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(export_dir):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, export_dir)
                zipf.write(file_path, arcname)
    
    shutil.rmtree(export_dir, ignore_errors=True)
    
    if errors:
        flash(f'Downloaded {downloaded}/{len(all_voicemails)} voicemails. Some failed.', 'warning')
    
    return send_file(
        zip_path,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f"{export_name}.zip"
    )


# ============================================================================
# FORWARD ROUTES
# ============================================================================

@app.route('/forward')
@login_required
def forward_page():
    """Page to configure forwarding"""
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    region_key = session.get('region_key')
    user_info = session.get('user_info', {})
    
    all_voicemails, total_count, error = get_all_voicemails_paginated(access_token, region_host)
    
    if error:
        flash(f'Error loading voicemails: {error}', 'warning')
        all_voicemails = []
    
    processed_voicemails = [format_voicemail(vm) for vm in all_voicemails]
    
    return render_template('forward.html',
                         user_info=user_info,
                         region=REGIONS.get(region_key, {}),
                         voicemails=processed_voicemails,
                         voicemail_count=len(processed_voicemails),
                         batch_size=BATCH_SIZE)


@app.route('/api/search/users')
@login_required
def api_search_users():
    """API endpoint to search for users"""
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    
    query = request.args.get('q', '').strip()
    if len(query) < 2:
        return jsonify({'users': [], 'error': 'Query too short'})
    
    users, error = search_users(access_token, region_host, query)
    
    if error:
        return jsonify({'users': [], 'error': error})
    
    formatted = []
    for user in users or []:
        formatted.append({
            'id': user.get('id'),
            'name': user.get('name'),
            'email': user.get('email'),
        })
    
    return jsonify({'users': formatted})


@app.route('/api/search/groups')
@login_required
def api_search_groups():
    """API endpoint to search for groups"""
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    
    query = request.args.get('q', '').strip()
    if len(query) < 2:
        return jsonify({'groups': [], 'error': 'Query too short'})
    
    groups, error = search_groups(access_token, region_host, query)
    
    if error:
        return jsonify({'groups': [], 'error': error})
    
    formatted = []
    for group in groups or []:
        formatted.append({
            'id': group.get('id'),
            'name': group.get('name'),
            'memberCount': group.get('memberCount', 0),
        })
    
    return jsonify({'groups': formatted})


@app.route('/api/forward', methods=['POST'])
@login_required
def api_forward_voicemails():
    """API endpoint to forward voicemails with batch processing"""
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data provided'}), 400
    
    voicemail_ids = data.get('voicemail_ids', [])
    target_id = data.get('target_id')
    target_type = data.get('target_type', 'user')
    
    if not voicemail_ids:
        return jsonify({'success': False, 'error': 'No voicemails selected'}), 400
    
    if not target_id:
        return jsonify({'success': False, 'error': 'No target selected'}), 400
    
    if target_type not in ['user', 'group']:
        return jsonify({'success': False, 'error': 'Invalid target type'}), 400
    
    # Process in batches
    results = process_voicemails_in_batches(
        access_token, region_host, voicemail_ids, 'forward',
        target_id=target_id, target_type=target_type
    )
    
    return jsonify({
        'success': results['failed'] == 0,
        'forwarded': results['success'],
        'failed': results['failed'],
        'total': results['total'],
        'errors': results['errors'][:10]  # Limit errors returned to prevent huge responses
    })


@app.route('/forward/single/<message_id>', methods=['POST'])
@login_required
def forward_single(message_id):
    """Forward a single voicemail (form submission)"""
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    
    target_id = request.form.get('target_id')
    target_type = request.form.get('target_type', 'user')
    target_name = request.form.get('target_name', 'Unknown')
    
    if not target_id:
        flash('Please select a recipient.', 'danger')
        return redirect(url_for('dashboard'))
    
    success, result = forward_voicemail_single(access_token, region_host, message_id, target_id, target_type)
    
    if success:
        flash(f'Voicemail forwarded successfully to {target_name}!', 'success')
    else:
        flash(f'Failed to forward voicemail: {result}', 'danger')
    
    return redirect(url_for('dashboard'))


# ============================================================================
# DELETE ROUTES
# ============================================================================

@app.route('/api/delete', methods=['POST'])
@login_required
def api_delete_voicemails():
    """API endpoint to delete voicemails with batch processing"""
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data provided'}), 400
    
    voicemail_ids = data.get('voicemail_ids', [])
    
    if not voicemail_ids:
        return jsonify({'success': False, 'error': 'No voicemails selected'}), 400
    
    # Process in batches
    results = process_voicemails_in_batches(
        access_token, region_host, voicemail_ids, 'delete'
    )
    
    return jsonify({
        'success': results['failed'] == 0,
        'deleted': results['success'],
        'failed': results['failed'],
        'total': results['total'],
        'errors': results['errors'][:10]  # Limit errors returned
    })


@app.route('/delete/single/<message_id>', methods=['POST'])
@login_required
def delete_single(message_id):
    """Delete a single voicemail"""
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    
    success, result = delete_voicemail_single(access_token, region_host, message_id)
    
    # Invalidate cache after deletion
    invalidate_voicemail_cache()
    
    if success:
        flash('Voicemail deleted successfully!', 'success')
    else:
        flash(f'Failed to delete voicemail: {result}', 'danger')
    
    return redirect(url_for('dashboard'))


@app.route('/delete')
@login_required
def delete_page():
    """Page to bulk delete voicemails"""
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    region_key = session.get('region_key')
    user_info = session.get('user_info', {})
    
    all_voicemails, total_count, error = get_all_voicemails_paginated(access_token, region_host)
    
    if error:
        flash(f'Error loading voicemails: {error}', 'warning')
        all_voicemails = []
    
    processed_voicemails = [format_voicemail(vm) for vm in all_voicemails]
    
    return render_template('delete.html',
                         user_info=user_info,
                         region=REGIONS.get(region_key, {}),
                         voicemails=processed_voicemails,
                         voicemail_count=len(processed_voicemails),
                         batch_size=BATCH_SIZE)


# ============================================================================
# OTHER API ROUTES
# ============================================================================

@app.route('/api/voicemails')
@login_required
def api_voicemails():
    """API endpoint to get voicemails as JSON with pagination"""
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    
    # Get pagination parameters
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', DISPLAY_PAGE_SIZE, type=int)
    
    # Limit page_size to reasonable values
    page_size = min(max(page_size, 10), 100)
    
    # Get all voicemails (with deleted filtered out)
    all_voicemails, total_count, error = get_all_voicemails_paginated(access_token, region_host)
    
    if error:
        return jsonify({'error': error, 'voicemails': []}), 500
    
    total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 1
    
    # Get slice for requested page
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    page_voicemails = all_voicemails[start_idx:end_idx]
    
    processed = []
    for vm in page_voicemails:
        processed.append({
            'id': vm.get('id'),
            'caller_name': vm.get('callerName', 'Unknown'),
            'caller_address': vm.get('callerAddress', ''),
            'created_date': vm.get('createdDate'),
            'duration_seconds': vm.get('audioRecordingDurationSeconds', 0),
            'read': vm.get('read', False),
        })
    
    return jsonify({
        'voicemails': processed,
        'count': len(processed),
        'total_count': total_count,
        'page': page,
        'page_size': page_size,
        'total_pages': total_pages,
        'has_next': page < total_pages,
        'has_prev': page > 1
    })


@app.route('/api/voicemails/stats')
@login_required
def api_voicemail_stats():
    """API endpoint to get voicemail statistics"""
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    
    stats = get_voicemail_stats_cached(access_token, region_host)
    
    return jsonify({
        'total_count': stats['total_count'],
        'total_duration_seconds': stats['total_duration'],
        'total_duration_minutes': round(stats['total_duration'] / 60, 1) if stats['total_duration'] else 0,
        'unread_count': stats['unread_count']
    })


@app.route('/api/voicemails/refresh')
@login_required
def api_refresh_voicemails():
    """Force refresh of voicemail data (invalidate cache)"""
    invalidate_voicemail_cache()
    return jsonify({'success': True, 'message': 'Cache invalidated'})


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
        'timestamp': datetime.now().isoformat(),
        'batch_size': BATCH_SIZE,
        'rate_limits': {
            'batch_delay': BATCH_DELAY,
            'operation_delay': OPERATION_DELAY
        }
    })

@app.route('/documentation')
def documentation():
    """Display documentation page"""
    return render_template('documentation.html')

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
    app.run(debug=True, host='127.0.0.1', port=5000)
