#!/usr/bin/env python3
"""
Genesys Cloud Voicemail Web Exporter - IMPROVED VERSION v7
==========================================================
NEW FEATURES:
1. Dedicated Download page with selection UI
2. Streaming progress for bulk downloads
3. 5-column dashboard with Delete All button
4. Better progress feedback during long operations

CHANGES FROM v6:
- Added /download route for bulk download page
- Added streaming progress via Server-Sent Events (SSE)
- Updated dashboard.html for 5-column layout
- Added templates/download.html
- Improved batch download with progress tracking
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
from collections import deque

from flask import (
    Flask, render_template, request, redirect, url_for, 
    session, flash, send_file, jsonify, Response, stream_with_context
)

# ============================================================================
# FLASK APP CONFIGURATION
# ============================================================================

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', secrets.token_hex(32))

app.config['SESSION_TYPE'] = 'filesystem'
app.config['PERMANENT_SESSION_LIFETIME'] = 3600

# ============================================================================
# GENESYS CONFIGURATION
# ============================================================================

CLIENT_ID = os.environ.get('GENESYS_CLIENT_ID', '')

if not CLIENT_ID:
    CLIENT_ID = ''

REDIRECT_URI = os.environ.get('REDIRECT_URI', 'http://127.0.0.1:5000/callback')

REGIONS = {
    "us_west": {"name": "US West", "host": "usw2.pure.cloud"}
}

TEMP_DIR = os.path.join(tempfile.gettempdir(), 'voicemail_exports')
os.makedirs(TEMP_DIR, exist_ok=True)

# ============================================================================
# CONFIGURATION
# ============================================================================

# FEATURE FLAGS
ENABLE_DOWNLOADS = True  # Set to False to disable all download functionality

API_PAGE_SIZE = 50  # Max items per API page (Genesys limit for /me/messages)
DISPLAY_PAGE_SIZE = 50  # Items per UI page

# Batch processing settings
BATCH_SIZE = 20
BATCH_DELAY = 3.0
OPERATION_DELAY = 0.2
RATE_LIMIT_BACKOFF = 10.0
MAX_RETRIES = 5
SUPER_BATCH_SIZE = 5
SUPER_BATCH_DELAY = 10.0

# Download-specific settings
DOWNLOAD_BATCH_SIZE = 10  # Smaller batches for downloads (media files are larger)
DOWNLOAD_OPERATION_DELAY = 0.5  # Longer delay for downloads

# Safety limit: 50 pages * 100 items = 5000 voicemails max
MAX_PAGES = 50

# Progress tracking for SSE
progress_data = {}

# ============================================================================
# PKCE HELPER FUNCTIONS
# ============================================================================

def generate_code_verifier(length=128):
    allowed_chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~'
    return ''.join(secrets.choice(allowed_chars) for _ in range(length))


def generate_code_challenge(code_verifier):
    code_hash = hashlib.sha256(code_verifier.encode('ascii')).digest()
    code_challenge = base64.urlsafe_b64encode(code_hash).decode('ascii')
    return code_challenge.rstrip('=')


def generate_state():
    return secrets.token_urlsafe(32)


# ============================================================================
# AUTHENTICATION HELPERS
# ============================================================================

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'access_token' not in session:
            flash('Please log in to access this page.', 'warning')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function


def exchange_code_for_token(auth_code, region_host, code_verifier):
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
# API REQUEST HELPER
# ============================================================================

def make_api_request(url, access_token, method='GET', data=None, retries=MAX_RETRIES):
    """Make API request with retry logic for rate limiting"""
    req = urllib.request.Request(url, method=method)
    req.add_header('Authorization', f'Bearer {access_token}')
    
    if data is not None:
        json_data = json.dumps(data).encode()
        req.data = json_data
        req.add_header('Content-Type', 'application/json')
    
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            if response.status == 204:
                return None, None
            return json.loads(response.read().decode()), None
            
    except urllib.request.HTTPError as e:
        # Handle rate limiting with retry
        if e.code == 429 and retries > 0:
            retry_after = e.headers.get('Retry-After', RATE_LIMIT_BACKOFF)
            try:
                wait_time = float(retry_after)
            except:
                wait_time = RATE_LIMIT_BACKOFF
            
            app.logger.warning(f"Rate limited (429). Waiting {wait_time}s. Retries left: {retries-1}")
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
    """Get current user information"""
    url = f"https://api.{region_host}/api/v2/users/me"
    data, error = make_api_request(url, access_token)
    
    if error:
        app.logger.error(f"Error getting user info: {error}")
        return None
    return data


def get_all_voicemails(access_token, region_host):
    """
    Get ALL voicemails with ACCURATE count.
    
    CRITICAL FIXES:
    1. Use /api/v2/voicemail/me/messages (not /messages) - more reliable
    2. API's 'total' field is STALE - updates in batches of 25
    3. API returns DELETED voicemails (soft-deleted, retained for 14 days)
    4. Must filter out deleted=true manually
    5. Use pageCount from API but count ACTUAL non-deleted entities
    
    Delete Retention: Voicemails marked as deleted are retained for 14 days
    (deleteRetentionPolicy.numberOfDays) before being permanently removed.
    
    Returns: (list of all active voicemails sorted by date desc, error)
    """
    all_voicemails = []
    page_number = 1
    api_page_count = None
    
    app.logger.info("Fetching all voicemails from /me/messages endpoint...")
    
    while page_number <= MAX_PAGES:
        # Use /me/messages endpoint - max pageSize is 50
        url = f"https://api.{region_host}/api/v2/voicemail/me/messages"
        params = {
            'pageSize': API_PAGE_SIZE,  # Max 50 for /me/messages
            'pageNumber': page_number
        }
        url_with_params = f"{url}?{urllib.parse.urlencode(params)}"
        
        data, error = make_api_request(url_with_params, access_token)
        
        if error:
            app.logger.error(f"Error fetching page {page_number}: {error}")
            if page_number == 1:
                return None, error
            break
        
        entities = data.get('entities', [])
        
        # Get pageCount from first request
        if page_number == 1:
            api_page_count = data.get('pageCount', 1)
            api_total = data.get('total', 0)
            app.logger.info(
                f"API reports: total={api_total}, pageCount={api_page_count} "
                f"(will filter deleted and count actual)"
            )
        
        # If page is empty, we're done
        if not entities:
            app.logger.debug(f"Page {page_number} is empty, stopping")
            break
        
        # Filter out DELETED voicemails
        page_active = []
        page_deleted = 0
        
        for vm in entities:
            is_deleted = (
                vm.get('deleted', False) or
                vm.get('state', '').upper() == 'DELETED' or
                vm.get('deletedDate') is not None
            )
            
            if is_deleted:
                page_deleted += 1
            else:
                page_active.append(vm)
        
        all_voicemails.extend(page_active)
        
        app.logger.debug(
            f"Page {page_number}/{api_page_count}: "
            f"{len(entities)} total, {len(page_active)} active, {page_deleted} deleted"
        )
        
        # Stop if we've processed all pages
        if api_page_count and page_number >= api_page_count:
            app.logger.debug(f"Reached last page ({api_page_count})")
            break
        
        page_number += 1
        time.sleep(0.05)  # Small delay between pages
    
    # Sort by date descending (newest first)
    all_voicemails.sort(
        key=lambda vm: vm.get('createdDate', '') or '', 
        reverse=True
    )
    
    # THE TRUE COUNT - from actual non-deleted entities
    true_count = len(all_voicemails)
    
    app.logger.info(
        f"✓ ACTUAL COUNT: {true_count} active voicemails "
        f"(fetched {page_number - 1} pages, filtered out deleted)"
    )
    
    return all_voicemails, None


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
                # Response contains media URI
                data = json.loads(response.read().decode())
                if 'mediaFileUri' in data:
                    media_req = urllib.request.Request(data['mediaFileUri'])
                    with urllib.request.urlopen(media_req, timeout=120) as media_response:
                        return media_response.read(), None
                return None, "No media URI in response"
            else:
                # Direct media download
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
    """Search for groups by name"""
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
# SINGLE VOICEMAIL OPERATIONS
# ============================================================================

def forward_voicemail_single(access_token, region_host, voicemail_id, target_id, target_type='user'):
    """Forward a single voicemail to a user or group"""
    url = f"https://api.{region_host}/api/v2/voicemail/messages"
    
    if target_type == 'group':
        body = {"groupId": target_id, "voicemailMessageId": voicemail_id}
    else:
        body = {"userId": target_id, "voicemailMessageId": voicemail_id}
    
    data, error = make_api_request(url, access_token, method='POST', data=body)
    
    if error:
        return False, error
    return True, data


def delete_voicemail_single(access_token, region_host, voicemail_id):
    """Delete a single voicemail"""
    url = f"https://api.{region_host}/api/v2/voicemail/messages/{voicemail_id}"
    
    data, error = make_api_request(url, access_token, method='DELETE')
    
    if error:
        return False, error
    return True, "Deleted"


# ============================================================================
# BATCH OPERATIONS
# ============================================================================

def process_voicemails_in_batches(access_token, region_host, voicemail_ids, operation, 
                                   target_id=None, target_type='user', progress_id=None):
    """
    Process voicemails in batches to avoid rate limiting.
    
    Args:
        operation: 'forward' or 'delete'
        target_id: Required for forward operation
        target_type: 'user' or 'group' for forward operation
        progress_id: Optional ID for progress tracking via SSE
    """
    results = {
        'success': 0,
        'failed': 0,
        'errors': [],
        'total': len(voicemail_ids),
        'processed': 0
    }
    
    total_ids = len(voicemail_ids)
    total_batches = (total_ids + BATCH_SIZE - 1) // BATCH_SIZE
    batches_since_super_break = 0
    consecutive_failures = 0
    
    app.logger.info(f"Starting {operation}: {total_ids} items in {total_batches} batches")
    
    # Initialize progress if tracking
    if progress_id:
        progress_data[progress_id] = {
            'processed': 0,
            'total': total_ids,
            'success': 0,
            'failed': 0,
            'status': 'processing'
        }
    
    for batch_start in range(0, total_ids, BATCH_SIZE):
        batch_end = min(batch_start + BATCH_SIZE, total_ids)
        batch = voicemail_ids[batch_start:batch_end]
        batch_num = (batch_start // BATCH_SIZE) + 1
        
        # Super batch break every SUPER_BATCH_SIZE batches
        if batches_since_super_break >= SUPER_BATCH_SIZE and batch_num > 1:
            app.logger.info(f"Super batch break: {SUPER_BATCH_DELAY}s...")
            time.sleep(SUPER_BATCH_DELAY)
            batches_since_super_break = 0
            consecutive_failures = 0
        
        batch_failures = 0
        
        # Process each item in the batch
        for idx, vm_id in enumerate(batch):
            try:
                if operation == 'forward':
                    success, result = forward_voicemail_single(
                        access_token, region_host, vm_id, target_id, target_type
                    )
                elif operation == 'delete':
                    success, result = delete_voicemail_single(access_token, region_host, vm_id)
                else:
                    success, result = False, "Unknown operation"
                
                if success:
                    results['success'] += 1
                    consecutive_failures = 0
                else:
                    results['failed'] += 1
                    results['errors'].append(f"{vm_id[:8]}: {result}")
                    batch_failures += 1
                    consecutive_failures += 1
                    
                    # If we have many consecutive failures, take a longer break
                    if consecutive_failures >= 3:
                        app.logger.warning("Multiple consecutive failures, taking extended break...")
                        time.sleep(SUPER_BATCH_DELAY * 2)
                        consecutive_failures = 0
                        batches_since_super_break = 0
                
                results['processed'] += 1
                
                # Update progress
                if progress_id:
                    progress_data[progress_id].update({
                        'processed': results['processed'],
                        'success': results['success'],
                        'failed': results['failed']
                    })
                
                # Delay between operations within a batch
                if idx < len(batch) - 1:
                    time.sleep(OPERATION_DELAY)
                    
            except Exception as e:
                results['failed'] += 1
                results['errors'].append(f"{vm_id[:8]}: {str(e)}")
                results['processed'] += 1
                
                if progress_id:
                    progress_data[progress_id].update({
                        'processed': results['processed'],
                        'failed': results['failed']
                    })
        
        batches_since_super_break += 1
        
        # Delay between batches (longer if batch had failures)
        if batch_end < total_ids:
            delay = BATCH_DELAY * 2 if batch_failures > 0 else BATCH_DELAY
            app.logger.debug(f"Batch {batch_num}/{total_batches} complete, waiting {delay}s...")
            time.sleep(delay)
    
    # Mark progress as complete
    if progress_id:
        progress_data[progress_id]['status'] = 'complete'
    
    app.logger.info(
        f"Operation complete: {results['success']} success, "
        f"{results['failed']} failed out of {results['total']}"
    )
    
    return results


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def format_voicemail(vm):
    """Format voicemail data for display"""
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
    """Generate safe filename for voicemail"""
    msg_id = voicemail.get('id', 'unknown')
    caller_name = voicemail.get('callerName', 'Unknown')
    created_date = voicemail.get('createdDate', '')
    
    if created_date:
        try:
            dt = datetime.fromisoformat(created_date.replace('Z', '+00:00'))
            date_str = dt.strftime('%Y%m%d_%H%M%S')
        except:
            date_str = 'unknown'
    else:
        date_str = 'unknown'
    
    # Sanitize caller name
    safe_caller = ''.join(
        c if c.isalnum() or c in ' -_' else '_' 
        for c in str(caller_name)
    )[:30]
    
    return f"{date_str}_{safe_caller}_{msg_id[:8]}.wav"


def format_duration(seconds):
    """Format duration in seconds to human readable string"""
    if not seconds:
        return "Unknown"
    
    minutes = int(seconds) // 60
    secs = int(seconds) % 60
    
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def format_datetime(date_string):
    """Format ISO datetime string to readable format"""
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
            if os.path.isfile(item_path) and now - os.path.getmtime(item_path) > 3600:
                os.remove(item_path)
            elif os.path.isdir(item_path) and now - os.path.getmtime(item_path) > 3600:
                shutil.rmtree(item_path, ignore_errors=True)
    except Exception as e:
        app.logger.error(f"Cleanup error: {e}")


# ============================================================================
# FLASK ROUTES
# ============================================================================

@app.route('/')
def index():
    """Home page with login form"""
    cleanup_old_exports()
    
    # If already logged in, redirect to dashboard
    if 'access_token' in session and 'user_info' in session:
        return redirect(url_for('dashboard'))
    
    return render_template('index.html', 
                         regions=REGIONS,
                         client_configured=bool(CLIENT_ID))


@app.route('/login', methods=['POST'])
def login():
    """Initiate OAuth login flow"""
    if not CLIENT_ID:
        flash('OAuth client not configured.', 'danger')
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
    
    # Store in session
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
        flash(f'Login failed: {request.args.get("error_description", error)}', 'danger')
        return redirect(url_for('index'))
    
    # Verify state
    state = request.args.get('state')
    if state != session.get('oauth_state'):
        flash('Invalid state parameter. Please try again.', 'danger')
        return redirect(url_for('index'))
    
    # Get authorization code
    auth_code = request.args.get('code')
    if not auth_code:
        flash('No authorization code received.', 'danger')
        return redirect(url_for('index'))
    
    # Get code verifier and region from session
    code_verifier = session.get('code_verifier')
    region_host = session.get('region_host')
    
    if not code_verifier or not region_host:
        flash('Session expired. Please try again.', 'danger')
        return redirect(url_for('index'))
    
    # Exchange code for token
    access_token, error = exchange_code_for_token(auth_code, region_host, code_verifier)
    
    if error:
        flash(f'Token exchange failed: {error}', 'danger')
        return redirect(url_for('index'))
    
    # Store access token
    session['access_token'] = access_token
    
    # Get user info
    user_info = get_user_info(access_token, region_host)
    if user_info:
        session['user_info'] = user_info
        flash(f'Welcome, {user_info.get("name", "User")}!', 'success')
    
    # Clean up temporary session data
    session.pop('code_verifier', None)
    session.pop('oauth_state', None)
    
    return redirect(url_for('dashboard'))


@app.route('/dashboard')
@login_required
def dashboard():
    """
    Main dashboard - shows voicemails with ACCURATE count.
    
    The count is based on actual entities fetched, NOT the API's stale 'total' field.
    """
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    region_key = session.get('region_key')
    user_info = session.get('user_info', {})
    
    # Get page number from query params
    page = request.args.get('page', 1, type=int)
    if page < 1:
        page = 1
    
    # Fetch ALL voicemails - count is from ACTUAL entities, NOT API's 'total'
    all_voicemails, error = get_all_voicemails(access_token, region_host)
    
    if error:
        flash(f'Error fetching voicemails: {error}', 'warning')
        all_voicemails = []
    
    # TRUE COUNT from actual fetched entities
    total_count = len(all_voicemails)
    
    # Calculate pagination
    total_pages = (
        (total_count + DISPLAY_PAGE_SIZE - 1) // DISPLAY_PAGE_SIZE 
        if total_count > 0 
        else 1
    )
    
    # Adjust page if out of range
    if page > total_pages:
        page = total_pages
    
    # Get voicemails for current page
    start_idx = (page - 1) * DISPLAY_PAGE_SIZE
    end_idx = start_idx + DISPLAY_PAGE_SIZE
    page_voicemails = all_voicemails[start_idx:end_idx]
    
    # Format voicemails for display
    processed_voicemails = [format_voicemail(vm) for vm in page_voicemails]
    
    # Calculate stats from actual data
    total_duration = sum(
        vm.get('audioRecordingDurationSeconds', 0) or 0 
        for vm in all_voicemails
    )
    unread_count = sum(1 for vm in all_voicemails if not vm.get('read', True))
    total_duration_minutes = round(total_duration / 60, 1) if total_duration else 0
    
    return render_template('dashboard.html',
                         user_info=user_info,
                         region=REGIONS.get(region_key, {}),
                         voicemails=processed_voicemails,
                         voicemail_count=total_count,  # ACCURATE count
                         total_duration_minutes=total_duration_minutes,
                         unread_count=unread_count,
                         current_page=page,
                         total_pages=total_pages,
                         has_prev=page > 1,
                         has_next=page < total_pages,
                         page_size=DISPLAY_PAGE_SIZE,
                         start_idx=start_idx + 1 if total_count > 0 else 0,
                         end_idx=min(end_idx, total_count),
                         enable_downloads=ENABLE_DOWNLOADS)


@app.route('/download/<message_id>')
@login_required
def download_single(message_id):
    """Download a single voicemail as WAV file"""
    # Check if downloads are enabled
    if not ENABLE_DOWNLOADS:
        flash('Download functionality is currently disabled.', 'warning')
        return redirect(url_for('dashboard'))
    
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    
    # Get voicemail info
    all_voicemails, _ = get_all_voicemails(access_token, region_host)
    voicemail = next((vm for vm in all_voicemails if vm.get('id') == message_id), None)
    
    if not voicemail:
        flash('Voicemail not found.', 'danger')
        return redirect(url_for('dashboard'))
    
    # Download media
    media_bytes, error = download_voicemail_media(access_token, region_host, message_id)
    
    if error:
        flash(f'Download failed: {error}', 'danger')
        return redirect(url_for('dashboard'))
    
    filename = format_filename(voicemail)
    
    return Response(
        media_bytes,
        mimetype='audio/wav',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )


@app.route('/download')
@login_required
def download_page():
    """Download page - bulk download with selection UI"""
    # Check if downloads are enabled
    if not ENABLE_DOWNLOADS:
        flash('Download functionality is currently disabled.', 'warning')
        return redirect(url_for('dashboard'))
    
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    region_key = session.get('region_key')
    user_info = session.get('user_info', {})
    
    # Get all voicemails
    all_voicemails, error = get_all_voicemails(access_token, region_host)
    
    if error:
        flash(f'Error fetching voicemails: {error}', 'warning')
        all_voicemails = []
    
    processed_voicemails = [format_voicemail(vm) for vm in all_voicemails]
    
    return render_template('download.html',
                         user_info=user_info,
                         region=REGIONS.get(region_key, {}),
                         voicemails=processed_voicemails,
                         voicemail_count=len(processed_voicemails),
                         batch_size=DOWNLOAD_BATCH_SIZE)


@app.route('/api/download-bulk', methods=['POST'])
@login_required
def api_download_bulk():
    """
    Bulk download with progress tracking via SSE
    
    This endpoint starts the download process and returns a progress_id
    that can be used to track progress via the /api/download-progress/<id> endpoint
    """
    # Check if downloads are enabled
    if not ENABLE_DOWNLOADS:
        return jsonify({'success': False, 'error': 'Download functionality is disabled'}), 403
    
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    user_info = session.get('user_info', {})
    
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data provided'}), 400
    
    voicemail_ids = data.get('voicemail_ids', [])
    
    if not voicemail_ids:
        return jsonify({'success': False, 'error': 'No voicemails selected'}), 400
    
    # Generate progress ID
    progress_id = secrets.token_urlsafe(16)
    
    # Start download in background (we'll handle this synchronously but with progress updates)
    # For a production app, you'd use Celery or similar for true background processing
    
    # Initialize progress
    progress_data[progress_id] = {
        'processed': 0,
        'total': len(voicemail_ids),
        'downloaded': 0,
        'failed': 0,
        'status': 'starting',
        'zip_ready': False,
        'zip_path': None
    }
    
    return jsonify({
        'success': True,
        'progress_id': progress_id,
        'total': len(voicemail_ids)
    })


@app.route('/api/download-progress/<progress_id>')
@login_required
def download_progress_stream(progress_id):
    """Server-Sent Events stream for download progress"""
    # Check if downloads are enabled
    if not ENABLE_DOWNLOADS:
        return jsonify({'error': 'Download functionality is disabled'}), 403
    
    def generate():
        """Generator function for SSE"""
        access_token = session.get('access_token')
        region_host = session.get('region_host')
        user_info = session.get('user_info', {})
        
        if progress_id not in progress_data:
            yield f"data: {json.dumps({'error': 'Invalid progress ID'})}\n\n"
            return
        
        # Get voicemail IDs from query param (passed when starting stream)
        voicemail_ids_param = request.args.get('voicemail_ids', '')
        if voicemail_ids_param:
            voicemail_ids = voicemail_ids_param.split(',')
        else:
            yield f"data: {json.dumps({'error': 'No voicemail IDs provided'})}\n\n"
            return
        
        # Get all voicemails to get metadata
        all_voicemails, _ = get_all_voicemails(access_token, region_host)
        selected_vms = [vm for vm in all_voicemails if vm.get('id') in voicemail_ids]
        
        # Create export directory
        user_name = user_info.get('name', 'Unknown').replace(' ', '_')
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        export_name = f"voicemails_{user_name}_{timestamp}"
        export_dir = os.path.join(TEMP_DIR, export_name)
        os.makedirs(export_dir, exist_ok=True)
        
        progress_data[progress_id]['status'] = 'downloading'
        
        # Download voicemails in batches
        downloaded = 0
        errors = []
        total_items = len(selected_vms)
        batches_since_super_break = 0
        
        for batch_start in range(0, total_items, DOWNLOAD_BATCH_SIZE):
            batch_end = min(batch_start + DOWNLOAD_BATCH_SIZE, total_items)
            batch = selected_vms[batch_start:batch_end]
            batch_num = (batch_start // DOWNLOAD_BATCH_SIZE) + 1
            
            # Super batch break
            if batches_since_super_break >= SUPER_BATCH_SIZE and batch_num > 1:
                time.sleep(SUPER_BATCH_DELAY)
                batches_since_super_break = 0
            
            # Download batch
            for idx, vm in enumerate(batch):
                msg_id = vm.get('id')
                filename = format_filename(vm)
                
                media_bytes, dl_error = download_voicemail_media(access_token, region_host, msg_id)
                
                if dl_error:
                    errors.append(f"{filename}: {dl_error}")
                    progress_data[progress_id]['failed'] += 1
                else:
                    filepath = os.path.join(export_dir, filename)
                    with open(filepath, 'wb') as f:
                        f.write(media_bytes)
                    downloaded += 1
                    progress_data[progress_id]['downloaded'] += 1
                
                progress_data[progress_id]['processed'] += 1
                
                # Send progress update
                progress_pct = int((progress_data[progress_id]['processed'] / total_items) * 100)
                yield f"data: {json.dumps({'progress': progress_pct, 'processed': progress_data[progress_id]['processed'], 'total': total_items, 'downloaded': downloaded, 'failed': len(errors)})}\n\n"
                
                # Delay between downloads
                if idx < len(batch) - 1:
                    time.sleep(DOWNLOAD_OPERATION_DELAY)
            
            batches_since_super_break += 1
            
            # Delay between batches
            if batch_end < total_items:
                time.sleep(BATCH_DELAY)
        
        # Save metadata
        metadata_file = os.path.join(export_dir, 'metadata.json')
        with open(metadata_file, 'w', encoding='utf-8') as f:
            json.dump({
                'exported_by': user_info.get('name'),
                'exported_at': datetime.now().isoformat(),
                'total_voicemails': len(selected_vms),
                'downloaded': downloaded,
                'errors': errors
            }, f, indent=2, default=str)
        
        # Create ZIP
        progress_data[progress_id]['status'] = 'zipping'
        yield f"data: {json.dumps({'status': 'zipping', 'progress': 95})}\n\n"
        
        zip_path = os.path.join(TEMP_DIR, f"{export_name}.zip")
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(export_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, export_dir)
                    zipf.write(file_path, arcname)
        
        # Clean up temp directory
        shutil.rmtree(export_dir, ignore_errors=True)
        
        # Mark complete
        progress_data[progress_id].update({
            'status': 'complete',
            'zip_ready': True,
            'zip_path': zip_path,
            'zip_filename': f"{export_name}.zip"
        })
        
        yield f"data: {json.dumps({'status': 'complete', 'progress': 100, 'downloaded': downloaded, 'failed': len(errors), 'zip_filename': f'{export_name}.zip'})}\n\n"
    
    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@app.route('/api/download-file/<progress_id>')
@login_required
def download_zip_file(progress_id):
    """Download the generated ZIP file"""
    # Check if downloads are enabled
    if not ENABLE_DOWNLOADS:
        flash('Download functionality is currently disabled.', 'warning')
        return redirect(url_for('dashboard'))
    
    if progress_id not in progress_data:
        flash('Download not found or expired.', 'danger')
        return redirect(url_for('download_page'))
    
    progress = progress_data[progress_id]
    
    if not progress.get('zip_ready') or not progress.get('zip_path'):
        flash('Download not ready yet.', 'warning')
        return redirect(url_for('download_page'))
    
    zip_path = progress['zip_path']
    zip_filename = progress['zip_filename']
    
    # Clean up progress data after download starts
    # (file will be cleaned up by cleanup_old_exports)
    progress_data.pop(progress_id, None)
    
    return send_file(
        zip_path,
        mimetype='application/zip',
        as_attachment=True,
        download_name=zip_filename
    )


@app.route('/download-all')
@login_required
def download_all():
    """Legacy endpoint - redirect to new download page or show disabled message"""
    if not ENABLE_DOWNLOADS:
        flash('Download functionality is currently disabled.', 'warning')
        return redirect(url_for('dashboard'))
    
    return redirect(url_for('download_page'))


@app.route('/forward')
@login_required
def forward_page():
    """Forward voicemails page"""
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    region_key = session.get('region_key')
    user_info = session.get('user_info', {})
    
    # Get all voicemails
    all_voicemails, error = get_all_voicemails(access_token, region_host)
    
    if error:
        flash(f'Error fetching voicemails: {error}', 'warning')
        all_voicemails = []
    
    processed_voicemails = [format_voicemail(vm) for vm in all_voicemails]
    
    return render_template('forward.html',
                         user_info=user_info,
                         region=REGIONS.get(region_key, {}),
                         voicemails=processed_voicemails,
                         voicemail_count=len(processed_voicemails),
                         batch_size=BATCH_SIZE)


@app.route('/delete')
@login_required
def delete_page():
    """Delete voicemails page"""
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    region_key = session.get('region_key')
    user_info = session.get('user_info', {})
    
    # Get all voicemails
    all_voicemails, error = get_all_voicemails(access_token, region_host)
    
    if error:
        flash(f'Error fetching voicemails: {error}', 'warning')
        all_voicemails = []
    
    processed_voicemails = [format_voicemail(vm) for vm in all_voicemails]
    
    return render_template('delete.html',
                         user_info=user_info,
                         region=REGIONS.get(region_key, {}),
                         voicemails=processed_voicemails,
                         voicemail_count=len(processed_voicemails),
                         batch_size=BATCH_SIZE)


# ============================================================================
# API ROUTES
# ============================================================================

@app.route('/api/search/users')
@login_required
def api_search_users():
    """Search users by name or email"""
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    
    query = request.args.get('q', '').strip()
    if len(query) < 2:
        return jsonify({'users': []})
    
    users, error = search_users(access_token, region_host, query)
    
    if error:
        return jsonify({'users': [], 'error': error})
    
    formatted = [
        {
            'id': u.get('id'), 
            'name': u.get('name'), 
            'email': u.get('email')
        } 
        for u in (users or [])
    ]
    
    return jsonify({'users': formatted})


@app.route('/api/search/groups')
@login_required
def api_search_groups():
    """Search groups by name"""
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    
    query = request.args.get('q', '').strip()
    if len(query) < 2:
        return jsonify({'groups': []})
    
    groups, error = search_groups(access_token, region_host, query)
    
    if error:
        return jsonify({'groups': [], 'error': error})
    
    formatted = [
        {
            'id': g.get('id'), 
            'name': g.get('name'), 
            'memberCount': g.get('memberCount', 0)
        } 
        for g in (groups or [])
    ]
    
    return jsonify({'groups': formatted})


@app.route('/api/forward', methods=['POST'])
@login_required
def api_forward_voicemails():
    """Forward voicemails in batches"""
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data provided'}), 400
    
    voicemail_ids = data.get('voicemail_ids', [])
    target_id = data.get('target_id')
    target_type = data.get('target_type', 'user')
    
    if not voicemail_ids or not target_id:
        return jsonify({'success': False, 'error': 'Missing required data'}), 400
    
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
        'errors': results['errors'][:10]  # Limit errors to first 10
    })


@app.route('/api/delete', methods=['POST'])
@login_required
def api_delete_voicemails():
    """Delete voicemails in batches"""
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
        'errors': results['errors'][:10]  # Limit errors to first 10
    })


@app.route('/api/voicemails')
@login_required
def api_voicemails():
    """Get voicemails list (JSON API) with accurate count"""
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', DISPLAY_PAGE_SIZE, type=int)
    page_size = min(max(page_size, 10), 100)  # Clamp between 10 and 100
    
    # Get all voicemails with accurate count
    all_voicemails, error = get_all_voicemails(access_token, region_host)
    
    if error:
        return jsonify({'error': error}), 500
    
    total_count = len(all_voicemails)
    total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 1
    
    # Get page of voicemails
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    page_voicemails = all_voicemails[start_idx:end_idx]
    
    # Minimal format for API
    processed = [{
        'id': vm.get('id'),
        'caller_name': vm.get('callerName', 'Unknown'),
        'caller_address': vm.get('callerAddress', ''),
        'created_date': vm.get('createdDate'),
        'duration_seconds': vm.get('audioRecordingDurationSeconds', 0),
        'read': vm.get('read', False),
    } for vm in page_voicemails]
    
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
    """Get voicemail statistics with accurate count"""
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    
    all_voicemails, error = get_all_voicemails(access_token, region_host)
    
    if error:
        return jsonify({'error': error}), 500
    
    total_count = len(all_voicemails)
    total_duration = sum(
        vm.get('audioRecordingDurationSeconds', 0) or 0 
        for vm in all_voicemails
    )
    unread_count = sum(1 for vm in all_voicemails if not vm.get('read', True))
    
    return jsonify({
        'total_count': total_count,
        'total_duration_seconds': total_duration,
        'total_duration_minutes': round(total_duration / 60, 1) if total_duration else 0,
        'unread_count': unread_count
    })


# ============================================================================
# UTILITY ROUTES
# ============================================================================

@app.route('/logout')
def logout():
    """Clear session and logout"""
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('index'))


@app.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'version': 'v7-improved-download-progress',
        'timestamp': datetime.now().isoformat()
    })


@app.route('/documentation')
def documentation():
    """Documentation page"""
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
    """Format datetime for templates"""
    return format_datetime(value)


@app.template_filter('duration')
def duration_filter(value):
    """Format duration for templates"""
    return format_duration(value)


# ============================================================================
# MAIN
# ============================================================================

if __name__ == '__main__':
    app.run(debug=True, host='127.0.0.1', port=5000)