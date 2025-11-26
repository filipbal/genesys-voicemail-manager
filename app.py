#!/usr/bin/env python3
"""
Genesys Cloud Voicemail Web Exporter - v10
==========================================
CHANGES FROM v9:
- Fixed pagination: use pageCount from API response, no hardcoded limit
- Dashboard shows only newest 20 records (preview mode)
- Download/Forward/Delete pages show ALL non-deleted records (concatenated)
- Batch sizes: 20 for forward/delete, 10 for download
- Accurate count from filtered pagination results
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
import threading
import uuid
from datetime import datetime
from functools import wraps
from collections import defaultdict

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, send_file, jsonify, Response
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

# API pagination
API_PAGE_SIZE = 10  # Records per API call

# Batch operation sizes
BATCH_SIZE = 20           # Forward/delete batch size
DOWNLOAD_BATCH_SIZE = 10  # Download batch size

# Rate limiting delays
OPERATION_DELAY = 0.25    # Delay between individual operations
BATCH_DELAY = 1.0         # Delay between batches
SUPER_BATCH_SIZE = 5      # Batches before super break
SUPER_BATCH_DELAY = 5.0   # Super break duration

# Download-specific
DOWNLOAD_OPERATION_DELAY = 0.5

# Batch download settings
BATCH_DOWNLOAD_SIZE = 20
BATCH_EXPIRY_SECONDS = 3600

# Progress tracking for SSE
progress_data = {}

# Storage for prepared batch downloads
prepared_batches = {}
batch_lock = threading.Lock()

# Queue for sequential batch processing
from queue import Queue
batch_queue = Queue()
batch_worker_running = False

# CONCURRENCY CONTROL: Per-user operation locks
user_operation_locks = defaultdict(threading.Lock)

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
# API REQUEST HELPER - NO RETRY LOGIC
# ============================================================================

def make_api_request(url, access_token, method='GET', data=None):
    """
    Make API request with NO retry logic.
    Fails fast on any error to prevent duplicates.
    """
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
        try:
            error_body = json.loads(e.read().decode())
            error_msg = error_body.get('message', str(error_body))
        except:
            error_msg = f"HTTP {e.code}: {e.reason}"
        
        if e.code == 429:
            app.logger.warning(f"Rate limit hit (429) - URL: {url}")
        
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
    Get ALL non-deleted voicemails from user's inbox using pagination.
    
    Logic:
    1. First API call gets pageCount from response
    2. Loop through all pages (1 to pageCount)
    3. Collect all entities, filter out deleted: true
    4. Return concatenated list sorted by date (newest first)
    
    Returns: (list of non-deleted voicemails, error string or None)
    """
    all_voicemails = []
    page_number = 1
    total_pages = None
    
    app.logger.info("Fetching user's voicemails from inbox...")
    
    while True:
        url = f"https://api.{region_host}/api/v2/voicemail/me/messages"
        params = {
            'pageSize': API_PAGE_SIZE,
            'pageNumber': page_number
        }
        url_with_params = f"{url}?{urllib.parse.urlencode(params)}"
        
        data, error = make_api_request(url_with_params, access_token)
        
        if error:
            app.logger.error(f"Error fetching page {page_number}: {error}")
            if page_number == 1:
                return None, error
            # If we already have some data, return what we have
            break
        
        entities = data.get('entities', [])
        
        # On first response, get the total page count
        if total_pages is None:
            total_pages = data.get('pageCount', 1)
            total_from_api = data.get('total', 0)
            app.logger.info(f"API reports {total_from_api} total records across {total_pages} pages")
        
        # Stop if no entities returned
        if not entities:
            app.logger.info(f"Empty page at {page_number}, stopping")
            break
        
        # Filter out deleted messages and add to collection
        non_deleted = [vm for vm in entities if not vm.get('deleted', False)]
        deleted_count = len(entities) - len(non_deleted)
        all_voicemails.extend(non_deleted)
        
        app.logger.debug(
            f"Page {page_number}/{total_pages}: "
            f"{len(non_deleted)} kept, {deleted_count} deleted skipped"
        )
        
        # Stop if we've reached the last page
        if page_number >= total_pages:
            app.logger.info(f"Reached last page {page_number}")
            break
        
        page_number += 1
        time.sleep(0.05)  # Small delay between API calls
    
    # Sort by date descending (newest first)
    all_voicemails.sort(key=lambda vm: vm.get('createdDate', ''), reverse=True)
    
    actual_count = len(all_voicemails)
    app.logger.info(f"✓ Total: {actual_count} non-deleted voicemails collected")
    
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
                data = json.loads(response.read().decode())
                if 'mediaFileUri' in data:
                    media_req = urllib.request.Request(data['mediaFileUri'])
                    with urllib.request.urlopen(media_req, timeout=120) as media_response:
                        return media_response.read(), None
                return None, "No media URI in response"
            else:
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
# BATCH OPERATIONS - NO RETRY LOGIC
# ============================================================================

def process_voicemails_in_batches(access_token, region_host, voicemail_ids, operation, 
                                   target_id=None, target_type='user', progress_id=None):
    """
    Process voicemails in batches with fail-fast approach (no retries).
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
    
    app.logger.info(f"Starting {operation}: {total_ids} items in {total_batches} batches")
    
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
        
        if batches_since_super_break >= SUPER_BATCH_SIZE and batch_num > 1:
            app.logger.info(f"Super batch break: {SUPER_BATCH_DELAY}s...")
            time.sleep(SUPER_BATCH_DELAY)
            batches_since_super_break = 0
        
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
                else:
                    results['failed'] += 1
                    results['errors'].append(f"VM {vm_id[:8]}: {result}")
                
                results['processed'] += 1
                
                if progress_id:
                    progress_data[progress_id].update({
                        'processed': results['processed'],
                        'success': results['success'],
                        'failed': results['failed']
                    })
                
                if idx < len(batch) - 1:
                    time.sleep(OPERATION_DELAY)
                    
            except Exception as e:
                results['failed'] += 1
                results['errors'].append(f"VM {vm_id[:8]}: Exception - {str(e)}")
                results['processed'] += 1
                
                if progress_id:
                    progress_data[progress_id].update({
                        'processed': results['processed'],
                        'failed': results['failed']
                    })
        
        batches_since_super_break += 1
        
        if batch_end < total_ids:
            app.logger.debug(f"Batch {batch_num}/{total_batches} complete, waiting {BATCH_DELAY}s...")
            time.sleep(BATCH_DELAY)
    
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
    """Clean up export files older than 1 hour and expired batch downloads"""
    try:
        now = time.time()
        for item in os.listdir(TEMP_DIR):
            item_path = os.path.join(TEMP_DIR, item)
            if os.path.isfile(item_path) and now - os.path.getmtime(item_path) > 3600:
                os.remove(item_path)
            elif os.path.isdir(item_path) and now - os.path.getmtime(item_path) > 3600:
                shutil.rmtree(item_path, ignore_errors=True)

        with batch_lock:
            expired_ids = []
            for batch_id, batch_info in prepared_batches.items():
                if now - batch_info.get('created', 0) > BATCH_EXPIRY_SECONDS:
                    expired_ids.append(batch_id)
                    zip_path = batch_info.get('zip_path')
                    if zip_path and os.path.exists(zip_path):
                        try:
                            os.remove(zip_path)
                        except:
                            pass

            for batch_id in expired_ids:
                del prepared_batches[batch_id]

            if expired_ids:
                app.logger.info(f"Cleaned up {len(expired_ids)} expired batch downloads")

    except Exception as e:
        app.logger.error(f"Cleanup error: {e}")


def process_single_batch(batch_id, voicemails, access_token, region_host, user_name, batch_num, total_batches):
    """Process a single batch - download voicemails and create ZIP."""
    try:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        export_name = f"voicemails_batch{batch_num}of{total_batches}_{user_name}_{timestamp}"
        export_dir = os.path.join(TEMP_DIR, f"{export_name}_temp")
        os.makedirs(export_dir, exist_ok=True)

        downloaded = 0
        errors = []
        total_items = len(voicemails)

        app.logger.info(f"Batch {batch_num}/{total_batches}: Starting download of {total_items} voicemails")

        for batch_start in range(0, total_items, DOWNLOAD_BATCH_SIZE):
            batch_end = min(batch_start + DOWNLOAD_BATCH_SIZE, total_items)
            batch = voicemails[batch_start:batch_end]

            for idx, vm in enumerate(batch):
                msg_id = vm.get('id')
                filename = format_filename(vm)

                media_bytes, dl_error = download_voicemail_media(access_token, region_host, msg_id)

                if dl_error:
                    errors.append(f"{filename}: {dl_error}")
                else:
                    filepath = os.path.join(export_dir, filename)
                    with open(filepath, 'wb') as f:
                        f.write(media_bytes)
                    downloaded += 1

                if idx < len(batch) - 1:
                    time.sleep(DOWNLOAD_OPERATION_DELAY)

            if batch_end < total_items:
                time.sleep(BATCH_DELAY)

        metadata_file = os.path.join(export_dir, 'metadata.json')
        with open(metadata_file, 'w', encoding='utf-8') as f:
            json.dump({
                'batch_number': batch_num,
                'total_batches': total_batches,
                'exported_by': user_name,
                'exported_at': datetime.now().isoformat(),
                'total_voicemails': total_items,
                'downloaded': downloaded,
                'errors': errors
            }, f, indent=2, default=str)

        zip_path = os.path.join(TEMP_DIR, f"{export_name}.zip")
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(export_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, export_dir)
                    zipf.write(file_path, arcname)

        shutil.rmtree(export_dir, ignore_errors=True)

        with batch_lock:
            if batch_id in prepared_batches:
                prepared_batches[batch_id].update({
                    'status': 'ready',
                    'zip_path': zip_path,
                    'downloaded': downloaded,
                    'errors': len(errors),
                    'total': total_items
                })

        app.logger.info(f"Batch {batch_num}/{total_batches}: Complete - {downloaded}/{total_items} downloaded")

    except Exception as e:
        app.logger.error(f"Batch {batch_num} preparation failed: {e}")
        with batch_lock:
            if batch_id in prepared_batches:
                prepared_batches[batch_id].update({
                    'status': 'failed',
                    'error': str(e)
                })


def batch_worker():
    """Worker thread that processes batches sequentially from the queue."""
    global batch_worker_running

    app.logger.info("Batch worker started")

    while True:
        try:
            job = batch_queue.get(timeout=5)

            if job is None:
                break

            batch_id, voicemails, access_token, region_host, user_name, batch_num, total_batches = job

            with batch_lock:
                if batch_id in prepared_batches:
                    prepared_batches[batch_id]['status'] = 'preparing'

            process_single_batch(batch_id, voicemails, access_token, region_host, user_name, batch_num, total_batches)

            batch_queue.task_done()

            time.sleep(1)

        except Exception as e:
            if str(e) != '':
                app.logger.error(f"Batch worker error: {e}")

            if batch_queue.empty():
                break

    batch_worker_running = False
    app.logger.info("Batch worker stopped")


def start_batch_worker():
    """Start the batch worker thread if not already running."""
    global batch_worker_running

    if not batch_worker_running:
        batch_worker_running = True
        worker_thread = threading.Thread(target=batch_worker)
        worker_thread.daemon = True
        worker_thread.start()
        app.logger.info("Started batch worker thread")


# ============================================================================
# FLASK ROUTES
# ============================================================================

@app.route('/')
def index():
    """Home page with login form"""
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
        flash('OAuth client not configured.', 'danger')
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
        flash(f'Login failed: {request.args.get("error_description", error)}', 'danger')
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
        flash(f'Token exchange failed: {error}', 'danger')
        return redirect(url_for('index'))
    
    session['access_token'] = access_token
    
    user_info = get_user_info(access_token, region_host)
    if user_info:
        session['user_info'] = user_info
        flash(f'Welcome, {user_info.get("name", "User")}!', 'success')
    
    session.pop('code_verifier', None)
    session.pop('oauth_state', None)
    
    return redirect(url_for('dashboard'))


@app.route('/dashboard')
@login_required
def dashboard():
    """Dashboard - shows newest 20 voicemails as preview"""
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    region_key = session.get('region_key')
    user_info = session.get('user_info', {})
    
    # Get all voicemails (concatenated from all pages, filtered)
    all_voicemails, error = get_all_voicemails(access_token, region_host)
    
    if error:
        flash(f'Error fetching voicemails: {error}', 'warning')
        return redirect(url_for('logout'))
    
    if not all_voicemails:
        all_voicemails = []
    
    # Total count from filtered results
    total_count = len(all_voicemails)
    
    # Show only newest 20 for preview
    preview_voicemails = all_voicemails[:20]
    processed_voicemails = [format_voicemail(vm) for vm in preview_voicemails]
    
    # Calculate total duration from ALL voicemails
    total_duration_seconds = sum(vm.get('audioRecordingDurationSeconds', 0) or 0 for vm in all_voicemails)
    total_duration_minutes = round(total_duration_seconds / 60, 1) if total_duration_seconds else 0
    
    return render_template('dashboard.html',
                         user_info=user_info,
                         region=REGIONS.get(region_key, {}),
                         voicemails=processed_voicemails,
                         voicemail_count=total_count,
                         preview_count=len(processed_voicemails),
                         total_duration_minutes=total_duration_minutes,
                         enable_downloads=ENABLE_DOWNLOADS)


@app.route('/download/<message_id>')
@login_required
def download_single(message_id):
    """Download a single voicemail as WAV file"""
    if not ENABLE_DOWNLOADS:
        flash('Download functionality is currently disabled.', 'warning')
        return redirect(url_for('dashboard'))
    
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    
    all_voicemails, _ = get_all_voicemails(access_token, region_host)
    voicemail = next((vm for vm in all_voicemails if vm.get('id') == message_id), None)
    
    if not voicemail:
        flash('Voicemail not found.', 'danger')
        return redirect(url_for('dashboard'))
    
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
    """Download page - shows ALL voicemails for bulk selection"""
    if not ENABLE_DOWNLOADS:
        flash('Download functionality is currently disabled.', 'warning')
        return redirect(url_for('dashboard'))
    
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    region_key = session.get('region_key')
    user_info = session.get('user_info', {})
    
    # Get ALL voicemails (concatenated)
    all_voicemails, error = get_all_voicemails(access_token, region_host)
    
    if error:
        flash(f'Error fetching voicemails: {error}', 'warning')
        all_voicemails = []
    
    # Format ALL for display
    processed_voicemails = [format_voicemail(vm) for vm in all_voicemails]
    total_count = len(processed_voicemails)
    
    return render_template('download.html',
                         user_info=user_info,
                         region=REGIONS.get(region_key, {}),
                         voicemails=processed_voicemails,
                         voicemail_count=total_count,
                         batch_size=DOWNLOAD_BATCH_SIZE,
                         batch_download_size=BATCH_DOWNLOAD_SIZE)


@app.route('/download-prepare', methods=['POST'])
@login_required
def download_prepare():
    """Prepare batch downloads"""
    if not ENABLE_DOWNLOADS:
        flash('Download functionality is currently disabled.', 'warning')
        return redirect(url_for('download_page'))

    access_token = session.get('access_token')
    region_host = session.get('region_host')
    user_info = session.get('user_info', {})

    data = request.get_json()
    if not data:
        flash('No data provided', 'danger')
        return redirect(url_for('download_page'))

    voicemail_ids = data.get('voicemail_ids', [])

    if not voicemail_ids:
        flash('No voicemails selected', 'warning')
        return redirect(url_for('download_page'))

    all_voicemails, error = get_all_voicemails(access_token, region_host)

    if error or not all_voicemails:
        app.logger.error(f"Failed to fetch voicemails: {error}")
        return jsonify({
            'success': False,
            'error': f'Failed to fetch voicemails: {error or "No voicemails returned"}'
        }), 500

    selected_vms = [vm for vm in all_voicemails if vm.get('id') in voicemail_ids]

    if not selected_vms:
        return jsonify({
            'success': False,
            'error': 'No matching voicemails found for the selected IDs'
        }), 400

    selected_vms.sort(key=lambda vm: vm.get('createdDate', '') or '', reverse=True)

    total_vms = len(selected_vms)
    num_batches = (total_vms + BATCH_DOWNLOAD_SIZE - 1) // BATCH_DOWNLOAD_SIZE

    user_name = user_info.get('name', 'Unknown').replace(' ', '_')
    manifest_id = str(uuid.uuid4())
    batch_ids = []

    app.logger.info(f"Preparing {num_batches} batches for {total_vms} voicemails")

    for i in range(num_batches):
        start_idx = i * BATCH_DOWNLOAD_SIZE
        end_idx = min(start_idx + BATCH_DOWNLOAD_SIZE, total_vms)
        batch_vms = selected_vms[start_idx:end_idx]
        batch_num = i + 1

        batch_id = str(uuid.uuid4())
        batch_ids.append(batch_id)

        with batch_lock:
            prepared_batches[batch_id] = {
                'status': 'queued',
                'created': time.time(),
                'batch_num': batch_num,
                'total_batches': num_batches,
                'total': len(batch_vms),
                'manifest_id': manifest_id,
                'filename': f"voicemails_batch{batch_num}of{num_batches}_{user_name}.zip"
            }

        batch_queue.put((batch_id, batch_vms, access_token, region_host, user_name, batch_num, num_batches))

    start_batch_worker()

    batches_info = []
    with batch_lock:
        for batch_id in batch_ids:
            batch = prepared_batches[batch_id]
            batches_info.append({
                'id': batch_id,
                'batch_num': batch['batch_num'],
                'total_batches': batch['total_batches'],
                'total': batch['total'],
                'status': batch['status'],
                'filename': batch['filename']
            })

    return jsonify({
        'success': True,
        'manifest_id': manifest_id,
        'batches': batches_info,
        'total_voicemails': total_vms,
        'num_batches': num_batches
    })


@app.route('/download-manifest')
@login_required
def download_manifest():
    """Display the manifest page with all batch download links"""
    if not ENABLE_DOWNLOADS:
        flash('Download functionality is currently disabled.', 'warning')
        return redirect(url_for('dashboard'))

    manifest_id = request.args.get('manifest_id')
    if not manifest_id:
        flash('No manifest ID provided', 'danger')
        return redirect(url_for('download_page'))

    user_info = session.get('user_info', {})
    region_key = session.get('region_key')

    batches_info = []
    with batch_lock:
        for batch_id, batch in prepared_batches.items():
            if batch.get('manifest_id') == manifest_id:
                batches_info.append({
                    'id': batch_id,
                    'batch_num': batch['batch_num'],
                    'total_batches': batch['total_batches'],
                    'total': batch['total'],
                    'status': batch['status'],
                    'filename': batch['filename'],
                    'downloaded': batch.get('downloaded', 0),
                    'errors': batch.get('errors', 0)
                })

    batches_info.sort(key=lambda x: x['batch_num'])

    if not batches_info:
        flash('Manifest not found or expired', 'warning')
        return redirect(url_for('download_page'))

    total_voicemails = sum(b['total'] for b in batches_info)

    return render_template('download_manifest.html',
                         user_info=user_info,
                         region=REGIONS.get(region_key, {}),
                         manifest_id=manifest_id,
                         batches=batches_info,
                         total_voicemails=total_voicemails,
                         expiry_minutes=BATCH_EXPIRY_SECONDS // 60)


@app.route('/download-batch/<batch_id>')
@login_required
def download_batch(batch_id):
    """Serve a prepared batch ZIP file"""
    if not ENABLE_DOWNLOADS:
        flash('Download functionality is currently disabled.', 'warning')
        return redirect(url_for('dashboard'))

    with batch_lock:
        batch = prepared_batches.get(batch_id)

    if not batch:
        flash('Batch not found or expired', 'danger')
        return redirect(url_for('download_page'))

    if batch['status'] == 'preparing':
        flash('Batch is still being prepared. Please wait and try again.', 'warning')
        return redirect(url_for('download_page'))

    if batch['status'] == 'failed':
        flash(f'Batch preparation failed: {batch.get("error", "Unknown error")}', 'danger')
        return redirect(url_for('download_page'))

    zip_path = batch.get('zip_path')
    if not zip_path or not os.path.exists(zip_path):
        flash('ZIP file not found. It may have expired.', 'danger')
        return redirect(url_for('download_page'))

    return send_file(
        zip_path,
        mimetype='application/zip',
        as_attachment=True,
        download_name=batch.get('filename', 'voicemails.zip')
    )


@app.route('/api/batch-status')
@login_required
def api_batch_status():
    """Get status of batches for a manifest"""
    manifest_id = request.args.get('manifest_id')
    if not manifest_id:
        return jsonify({'error': 'No manifest ID provided'}), 400

    batches_info = []
    with batch_lock:
        for batch_id, batch in prepared_batches.items():
            if batch.get('manifest_id') == manifest_id:
                batches_info.append({
                    'id': batch_id,
                    'batch_num': batch['batch_num'],
                    'status': batch['status'],
                    'downloaded': batch.get('downloaded', 0),
                    'errors': batch.get('errors', 0),
                    'total': batch['total']
                })

    batches_info.sort(key=lambda x: x['batch_num'])

    all_ready = all(b['status'] == 'ready' for b in batches_info)
    any_failed = any(b['status'] == 'failed' for b in batches_info)

    return jsonify({
        'batches': batches_info,
        'all_ready': all_ready,
        'any_failed': any_failed
    })


@app.route('/forward')
@login_required
def forward_page():
    """Forward page - shows ALL voicemails for selection"""
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    region_key = session.get('region_key')
    user_info = session.get('user_info', {})
    
    # Get ALL voicemails (concatenated)
    all_voicemails, error = get_all_voicemails(access_token, region_host)
    
    if error:
        flash(f'Error fetching voicemails: {error}', 'warning')
        all_voicemails = []
    
    processed_voicemails = [format_voicemail(vm) for vm in all_voicemails]
    total_count = len(processed_voicemails)
    
    return render_template('forward.html',
                         user_info=user_info,
                         region=REGIONS.get(region_key, {}),
                         voicemails=processed_voicemails,
                         voicemail_count=total_count,
                         batch_size=BATCH_SIZE)


@app.route('/delete')
@login_required
def delete_page():
    """Delete page - shows ALL voicemails for selection"""
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    region_key = session.get('region_key')
    user_info = session.get('user_info', {})
    
    # Get ALL voicemails (concatenated)
    all_voicemails, error = get_all_voicemails(access_token, region_host)
    
    if error:
        flash(f'Error fetching voicemails: {error}', 'warning')
        all_voicemails = []
    
    processed_voicemails = [format_voicemail(vm) for vm in all_voicemails]
    total_count = len(processed_voicemails)
    
    return render_template('delete.html',
                         user_info=user_info,
                         region=REGIONS.get(region_key, {}),
                         voicemails=processed_voicemails,
                         voicemail_count=total_count,
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
    user_id = session.get('user_info', {}).get('id')
    
    if not user_id:
        return jsonify({'success': False, 'error': 'User not identified'}), 401
    
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data provided'}), 400
    
    voicemail_ids = data.get('voicemail_ids', [])
    target_id = data.get('target_id')
    target_type = data.get('target_type', 'user')
    
    if not voicemail_ids or not target_id:
        return jsonify({'success': False, 'error': 'Missing required data'}), 400
    
    user_lock = user_operation_locks[user_id]
    
    if not user_lock.acquire(blocking=False):
        return jsonify({
            'success': False,
            'error': 'Another forward operation is already in progress. Please wait for it to complete.'
        }), 409
    
    try:
        results = process_voicemails_in_batches(
            access_token, region_host, voicemail_ids, 'forward',
            target_id=target_id, target_type=target_type
        )
        
        return jsonify({
            'success': results['failed'] == 0,
            'forwarded': results['success'],
            'failed': results['failed'],
            'total': results['total'],
            'errors': results['errors'][:20]
        })
    finally:
        user_lock.release()


@app.route('/api/delete', methods=['POST'])
@login_required
def api_delete_voicemails():
    """Delete voicemails in batches"""
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    user_id = session.get('user_info', {}).get('id')
    
    if not user_id:
        return jsonify({'success': False, 'error': 'User not identified'}), 401
    
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data provided'}), 400
    
    voicemail_ids = data.get('voicemail_ids', [])
    
    if not voicemail_ids:
        return jsonify({'success': False, 'error': 'No voicemails selected'}), 400
    
    user_lock = user_operation_locks[user_id]
    
    if not user_lock.acquire(blocking=False):
        return jsonify({
            'success': False,
            'error': 'Another delete operation is already in progress. Please wait for it to complete.'
        }), 409
    
    try:
        results = process_voicemails_in_batches(
            access_token, region_host, voicemail_ids, 'delete'
        )
        
        return jsonify({
            'success': results['failed'] == 0,
            'deleted': results['success'],
            'failed': results['failed'],
            'total': results['total'],
            'errors': results['errors'][:20]
        })
    finally:
        user_lock.release()


@app.route('/api/voicemails')
@login_required
def api_voicemails():
    """Get all voicemails list (JSON API)"""
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    
    all_voicemails, error = get_all_voicemails(access_token, region_host)
    
    if error:
        return jsonify({'error': error}), 500
    
    processed = [{
        'id': vm.get('id'),
        'caller_name': vm.get('callerName', 'Unknown'),
        'caller_address': vm.get('callerAddress', ''),
        'created_date': vm.get('createdDate'),
        'duration_seconds': vm.get('audioRecordingDurationSeconds', 0),
        'read': vm.get('read', False),
    } for vm in all_voicemails]
    
    return jsonify({
        'voicemails': processed,
        'total_count': len(processed)
    })


@app.route('/api/voicemails/stats')
@login_required
def api_voicemail_stats():
    """Get voicemail statistics"""
    access_token = session.get('access_token')
    region_host = session.get('region_host')
    
    all_voicemails, error = get_all_voicemails(access_token, region_host)
    
    if error:
        return jsonify({'error': error}), 500
    
    total_count = len(all_voicemails)
    unread_count = sum(1 for vm in all_voicemails if not vm.get('read', True))
    
    total_duration = sum(
        vm.get('audioRecordingDurationSeconds', 0) or 0 
        for vm in all_voicemails
    )
    
    return jsonify({
        'total_count': total_count,
        'unread_count': unread_count,
        'total_duration_seconds': total_duration,
        'total_duration_minutes': round(total_duration / 60, 1) if total_duration else 0
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
        'version': 'v10-pagination-fix',
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
