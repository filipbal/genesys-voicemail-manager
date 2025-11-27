#!/usr/bin/env python3
"""
Genesys Cloud Voicemail Manager - v15
==========================================
CHANGES FROM v14:
- Added modified_date column to show forwarding time
- Added original_caller and forwarded_by info
- Updated filename format to include both dates
- Enhanced metadata export with forwarding chain info
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
# RATE LIMITING & CONFIGURATION
# ============================================================================

# FEATURE FLAGS
ENABLE_DOWNLOADS = False

# API Settings
API_PAGE_SIZE = 100

# PROACTIVE DELAYS (Seconds)
API_DELAY_GET = 1.0
API_DELAY_WRITE = 1.0

# Batch settings
BATCH_SIZE = 20
DOWNLOAD_BATCH_SIZE = 10
SUPER_BATCH_SIZE = 5
SUPER_BATCH_DELAY = 2.0

DOWNLOAD_OPERATION_DELAY = 0.5
BATCH_DOWNLOAD_SIZE = 20
BATCH_EXPIRY_SECONDS = 3600

# State management
progress_data = {}
prepared_batches = {}
batch_lock = threading.Lock()
user_operation_locks = defaultdict(threading.Lock)

# Queue for batch worker
from queue import Queue
batch_queue = Queue()
batch_worker_running = False

# ============================================================================
# AUTHENTICATION HELPERS
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
# API REQUEST HELPER - RETRY FAILSAFE
# ============================================================================

def make_api_request(url, access_token, method='GET', data=None):
	max_retries = 3
	base_backoff = 2.0
	
	req = urllib.request.Request(url, method=method)
	req.add_header('Authorization', f'Bearer {access_token}')
	
	if data is not None:
		json_data = json.dumps(data).encode()
		req.data = json_data
		req.add_header('Content-Type', 'application/json')
	
	for attempt in range(max_retries + 1):
		try:
			with urllib.request.urlopen(req, timeout=60) as response:
				if response.status == 204:
					return None, None
				return json.loads(response.read().decode()), None
				
		except urllib.request.HTTPError as e:
			if e.code == 429:
				retry_after = int(e.headers.get('Retry-After', base_backoff * (2 ** attempt)))
				app.logger.warning(f"Rate limit (429). Retrying in {retry_after}s... (Attempt {attempt+1})")
				time.sleep(retry_after)
				continue
			
			if 500 <= e.code < 600 and attempt < max_retries:
				sleep_time = base_backoff * (2 ** attempt)
				app.logger.warning(f"Server error {e.code}. Retrying in {sleep_time}s...")
				time.sleep(sleep_time)
				continue

			try:
				error_body = json.loads(e.read().decode())
				error_msg = error_body.get('message', str(error_body))
			except:
				error_msg = f"HTTP {e.code}: {e.reason}"
			
			return None, error_msg
			
		except Exception as e:
			if attempt < max_retries:
				time.sleep(1)
				continue
			return None, str(e)
			
	return None, "Max retries exceeded"

# ============================================================================
# GENESYS API FUNCTIONS
# ============================================================================

def get_user_info(access_token, region_host):
	url = f"https://api.{region_host}/api/v2/users/me"
	data, error = make_api_request(url, access_token)
	if error:
		app.logger.error(f"Error getting user info: {error}")
		return None
	return data

def get_all_voicemails(access_token, region_host, user_id=None):
	all_entities = []
	
	if not user_id:
		user_info = get_user_info(access_token, region_host)
		if not user_info:
			return None, "Could not get user info"
		user_id = user_info.get('id')
	
	url = f"https://api.{region_host}/api/v2/voicemail/search"
	page_number = 1
	page_size = API_PAGE_SIZE
	
	search_body = {
		"pageSize": page_size,
		"pageNumber": page_number,
		"query": [
			{
				"fields": ["owner"],
				"type": "EXACT",
				"value": "user"
			},
			{
				"fields": ["ownerId"],
				"type": "EXACT",
				"value": user_id
			}
		]
	}
	
	app.logger.info(f"Fetching voicemails for user {user_id} via POST search...")
	
	total_expected = None
	
	while True:
		search_body["pageNumber"] = page_number
		
		data, error = make_api_request(url, access_token, method='POST', data=search_body)
		
		if error:
			app.logger.error(f"Error fetching page {page_number}: {error}")
			if page_number == 1:
				return None, error
			break
		
		results = data.get('results', [])
		all_entities.extend(results)
		
		if total_expected is None:
			total_expected = data.get('total', 0)
			app.logger.info(f"API reports {total_expected} total voicemails")
		
		page_count = data.get('pageCount', 0)
		
		if page_number >= page_count or not results:
			break
			
		page_number += 1
		time.sleep(API_DELAY_GET)
	
	unique_map = {v['id']: v for v in all_entities}
	unique_entities = list(unique_map.values())
	
	active_voicemails = [v for v in unique_entities if not v.get('deleted', False)]
	
	active_voicemails.sort(key=lambda vm: vm.get('createdDate', ''), reverse=True)
	
	app.logger.info(
		f"Fetch Complete: {len(all_entities)} raw, "
		f"{len(unique_entities)} unique, "
		f"{len(active_voicemails)} active."
	)
	
	return active_voicemails, None

def download_voicemail_media(access_token, region_host, message_id):
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
				
	except Exception as e:
		return None, str(e)

def search_users(access_token, region_host, query):
	url = f"https://api.{region_host}/api/v2/users/search"
	search_body = {
		"pageSize": 25,
		"pageNumber": 1,
		"query": [{"type": "QUERY_STRING", "fields": ["name", "email"], "value": f"*{query}*"}]
	}
	data, error = make_api_request(url, access_token, method='POST', data=search_body)
	if error: return None, error
	return data.get('results', []), None

def search_groups(access_token, region_host, query):
	url = f"https://api.{region_host}/api/v2/groups/search"
	search_body = {
		"pageSize": 25,
		"pageNumber": 1,
		"query": [{"type": "STARTS_WITH", "fields": ["name"], "value": query}]
	}
	data, error = make_api_request(url, access_token, method='POST', data=search_body)
	if error: return None, error
	return data.get('results', []), None

# ============================================================================
# OPERATIONS
# ============================================================================

def process_voicemails_in_batches(access_token, region_host, voicemail_ids, operation, 
                                   target_id=None, target_type='user', progress_id=None):
    app.logger.info(f"=== BATCH {operation.upper()} START ===")
    app.logger.info(f"Input IDs: {len(voicemail_ids)}, Unique: {len(set(voicemail_ids))}")
    
    original_count = len(voicemail_ids)
    voicemail_ids = list(dict.fromkeys(voicemail_ids))
    if len(voicemail_ids) != original_count:
        app.logger.warning(f"Removed {original_count - len(voicemail_ids)} duplicate IDs from input")
    
    results = {'success': 0, 'failed': 0, 'errors': [], 'total': len(voicemail_ids), 'processed': 0}
    
    total_ids = len(voicemail_ids)
    
    if progress_id:
        progress_data[progress_id] = {'processed': 0, 'total': total_ids, 'success': 0, 'failed': 0, 'status': 'processing'}
    
    for i, vm_id in enumerate(voicemail_ids):
        if i > 0 and i % (BATCH_SIZE * SUPER_BATCH_SIZE) == 0:
            app.logger.info(f"Super batch break at item {i}, sleeping {SUPER_BATCH_DELAY}s...")
            time.sleep(SUPER_BATCH_DELAY)
        
        success = False
        msg = ""
        
        try:
            if operation == 'forward':
                url = f"https://api.{region_host}/api/v2/voicemail/messages"
                body = {"voicemailMessageId": vm_id}
                if target_type == 'group':
                    body["groupId"] = target_id
                else:
                    body["userId"] = target_id
                
                response_data, err = make_api_request(url, access_token, 'POST', body)
                success = (err is None)
                msg = err
                
                if success and response_data:
                    new_id = response_data.get('id', 'unknown')
                    app.logger.debug(f"Forward OK: {vm_id[:8]} -> new ID: {new_id[:8] if new_id != 'unknown' else 'unknown'}")
                elif not success:
                    app.logger.error(f"Forward FAIL: {vm_id[:8]} - {msg}")
            
            elif operation == 'delete':
                url = f"https://api.{region_host}/api/v2/voicemail/messages/{vm_id}"
                _, err = make_api_request(url, access_token, 'DELETE')
                success = (err is None)
                msg = err
                
                if not success:
                    app.logger.error(f"Delete FAIL: {vm_id[:8]} - {msg}")
            
            if success:
                results['success'] += 1
            else:
                results['failed'] += 1
                results['errors'].append(f"VM {vm_id[:8]}: {msg}")
                
        except Exception as e:
            results['failed'] += 1
            results['errors'].append(f"VM {vm_id[:8]}: {str(e)}")
            app.logger.exception(f"Exception processing {vm_id[:8]}: {e}")
            
        results['processed'] += 1
        
        if progress_id:
            progress_data[progress_id].update({
                'processed': results['processed'],
                'success': results['success'],
                'failed': results['failed']
            })
        
        if (i + 1) % 50 == 0:
            app.logger.info(f"Progress: {i+1}/{total_ids} - Success: {results['success']}, Failed: {results['failed']}")
            
        time.sleep(API_DELAY_WRITE)
    
    if progress_id:
        progress_data[progress_id]['status'] = 'complete'
    
    app.logger.info(f"=== BATCH {operation.upper()} END === Total: {total_ids}, Success: {results['success']}, Failed: {results['failed']}")
    
    return results

# ============================================================================
# HELPERS
# ============================================================================

def format_voicemail(vm):
	"""Format voicemail with full date and forwarding info"""
	created = vm.get('createdDate', '')
	modified = vm.get('modifiedDate', '')
	
	# Determine if this is a forwarded message
	copied_from = vm.get('copiedFrom')
	caller_user = vm.get('callerUser')
	
	# Original caller info
	original_caller = vm.get('callerName', 'Unknown')
	original_caller_address = vm.get('callerAddress', '')
	
	# Forwarding info
	is_forwarded = copied_from is not None
	forwarded_by = None
	forwarded_date = None
	
	if is_forwarded and caller_user:
		# callerUser contains info about who forwarded it
		forwarded_by = caller_user.get('name', 'Unknown')
		# For forwarded messages, createdDate is when it was forwarded
		forwarded_date = created
	
	return {
		'id': vm.get('id'),
		'caller_name': original_caller,
		'caller_address': original_caller_address,
		'created_date': format_datetime(created),
		'created_date_raw': created,
		'modified_date': format_datetime(modified) if modified else None,
		'modified_date_raw': modified,
		'duration': format_duration(vm.get('audioRecordingDurationSeconds')),
		'duration_seconds': vm.get('audioRecordingDurationSeconds'),
		'read': vm.get('read', False),
		'is_forwarded': is_forwarded,
		'forwarded_by': forwarded_by,
		'forwarded_date': format_datetime(forwarded_date) if forwarded_date else None,
		'filename': format_filename(vm),
		# Raw data for metadata export
		'raw_caller_user': caller_user,
		'raw_copied_from': copied_from,
	}

def format_filename(voicemail):
	"""Generate filename with both created and modified dates if different"""
	msg_id = voicemail.get('id', 'unknown')
	caller_name = voicemail.get('callerName', 'Unknown')
	created_date = voicemail.get('createdDate', '')
	modified_date = voicemail.get('modifiedDate', '')
	
	# Format created date
	created_str = 'unknown'
	if created_date:
		try:
			dt = datetime.fromisoformat(created_date.replace('Z', '+00:00'))
			created_str = dt.strftime('%Y%m%d_%H%M%S')
		except:
			pass
	
	# Format modified date if different from created
	modified_str = None
	if modified_date and modified_date != created_date:
		try:
			dt = datetime.fromisoformat(modified_date.replace('Z', '+00:00'))
			modified_str = dt.strftime('%Y%m%d_%H%M%S')
		except:
			pass
	
	# Safe caller name
	safe_caller = ''.join(c if c.isalnum() or c in ' -_' else '_' for c in str(caller_name))[:30]
	
	# Build filename
	if modified_str and modified_str != created_str:
		# Format: created_modified_caller_id.wav
		return f"{created_str}_fwd{modified_str}_{safe_caller}_{msg_id[:8]}.wav"
	else:
		return f"{created_str}_{safe_caller}_{msg_id[:8]}.wav"

def format_duration(seconds):
	if not seconds: return "Unknown"
	minutes = int(seconds) // 60
	secs = int(seconds) % 60
	return f"{minutes}m {secs}s" if minutes > 0 else f"{secs}s"

def format_datetime(date_string):
	if not date_string: return "Unknown"
	try:
		dt = datetime.fromisoformat(date_string.replace('Z', '+00:00'))
		return dt.strftime('%Y-%m-%d %H:%M:%S')
	except: return date_string

def format_datetime_short(date_string):
	"""Shorter format for table display"""
	if not date_string: return "-"
	try:
		dt = datetime.fromisoformat(date_string.replace('Z', '+00:00'))
		return dt.strftime('%m/%d/%y %H:%M')
	except: return date_string

def cleanup_old_exports():
	try:
		now = time.time()
		for item in os.listdir(TEMP_DIR):
			item_path = os.path.join(TEMP_DIR, item)
			if os.path.isfile(item_path) and now - os.path.getmtime(item_path) > 3600:
				os.remove(item_path)
			elif os.path.isdir(item_path) and now - os.path.getmtime(item_path) > 3600:
				shutil.rmtree(item_path, ignore_errors=True)
				
		with batch_lock:
			expired = [bid for bid, b in prepared_batches.items() if now - b.get('created', 0) > BATCH_EXPIRY_SECONDS]
			for bid in expired:
				zip_path = prepared_batches[bid].get('zip_path')
				if zip_path and os.path.exists(zip_path): os.remove(zip_path)
				del prepared_batches[bid]
	except Exception as e:
		app.logger.error(f"Cleanup error: {e}")

def build_voicemail_metadata(vm, user_name):
	"""Build detailed metadata for a voicemail including forwarding chain"""
	formatted = format_voicemail(vm)
	
	metadata = {
		'id': vm.get('id'),
		'filename': formatted['filename'],
		'original_caller': {
			'name': vm.get('callerName', 'Unknown'),
			'address': vm.get('callerAddress', ''),
		},
		'dates': {
			'created': vm.get('createdDate'),
			'modified': vm.get('modifiedDate'),
			'created_formatted': formatted['created_date'],
			'modified_formatted': formatted['modified_date'],
		},
		'duration_seconds': vm.get('audioRecordingDurationSeconds'),
		'duration_formatted': formatted['duration'],
		'read': vm.get('read', False),
		'is_forwarded': formatted['is_forwarded'],
	}
	
	# Add forwarding info if applicable
	if formatted['is_forwarded']:
		metadata['forwarding'] = {
			'forwarded_by': formatted['forwarded_by'],
			'forwarded_date': formatted['forwarded_date'],
			'source_message_id': vm.get('copiedFrom', {}).get('id') if vm.get('copiedFrom') else None,
		}
		if formatted['raw_caller_user']:
			metadata['forwarding']['forwarded_by_user'] = {
				'id': formatted['raw_caller_user'].get('id'),
				'name': formatted['raw_caller_user'].get('name'),
				'email': formatted['raw_caller_user'].get('email'),
			}
	
	return metadata

# Batch Download Worker
def process_single_batch(batch_id, voicemails, access_token, region_host, user_name, batch_num, total_batches):
	try:
		timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
		export_name = f"voicemails_batch{batch_num}of{total_batches}_{user_name}_{timestamp}"
		export_dir = os.path.join(TEMP_DIR, f"{export_name}_temp")
		os.makedirs(export_dir, exist_ok=True)

		downloaded = 0
		errors = []
		total_items = len(voicemails)
		voicemail_metadata = []

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
					
					# Build metadata for this voicemail
					voicemail_metadata.append(build_voicemail_metadata(vm, user_name))
					
				time.sleep(DOWNLOAD_OPERATION_DELAY)
			time.sleep(2.0)  # Batch delay

		# Create metadata.json with enhanced info
		metadata = {
			'export_info': {
				'exported_by': user_name,
				'exported_at': datetime.now().isoformat(),
				'batch_number': batch_num,
				'total_batches': total_batches,
			},
			'summary': {
				'total_in_batch': total_items,
				'downloaded': downloaded,
				'errors_count': len(errors),
			},
			'voicemails': voicemail_metadata,
			'errors': errors if errors else None,
		}
		
		metadata_path = os.path.join(export_dir, 'metadata.json')
		with open(metadata_path, 'w') as f:
			json.dump(metadata, f, indent=2)

		# ZIP creation
		zip_path = os.path.join(TEMP_DIR, f"{export_name}.zip")
		with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
			for root, dirs, files in os.walk(export_dir):
				for file in files:
					file_path = os.path.join(root, file)
					zipf.write(file_path, os.path.relpath(file_path, export_dir))
		
		shutil.rmtree(export_dir, ignore_errors=True)

		with batch_lock:
			if batch_id in prepared_batches:
				prepared_batches[batch_id].update({
					'status': 'ready',
					'zip_path': zip_path,
					'downloaded': downloaded,
					'errors': len(errors)
				})

	except Exception as e:
		with batch_lock:
			if batch_id in prepared_batches:
				prepared_batches[batch_id].update({'status': 'failed', 'error': str(e)})

def batch_worker():
	global batch_worker_running
	while True:
		try:
			job = batch_queue.get(timeout=5)
			if job is None: break
			batch_id, voicemails, access_token, region_host, user_name, batch_num, total_batches = job
			with batch_lock:
				if batch_id in prepared_batches:
					prepared_batches[batch_id]['status'] = 'preparing'
			process_single_batch(batch_id, voicemails, access_token, region_host, user_name, batch_num, total_batches)
			batch_queue.task_done()
		except: 
			if batch_queue.empty(): break
	batch_worker_running = False

def start_batch_worker():
	global batch_worker_running
	if not batch_worker_running:
		batch_worker_running = True
		t = threading.Thread(target=batch_worker)
		t.daemon = True
		t.start()

# ============================================================================
# FLASK ROUTES
# ============================================================================

@app.route('/')
def index():
	cleanup_old_exports()
	if 'access_token' in session and 'user_info' in session:
		return redirect(url_for('dashboard'))
	return render_template('index.html', regions=REGIONS, client_configured=bool(CLIENT_ID))

@app.route('/login', methods=['POST'])
def login():
	if not CLIENT_ID:
		flash('OAuth client not configured. Check CICS.json or env vars.', 'danger')
		return redirect(url_for('index'))
	
	region_key = request.form.get('region')
	if region_key not in REGIONS:
		return redirect(url_for('index'))
	
	region = REGIONS[region_key]
	verifier = generate_code_verifier()
	state = generate_state()
	
	session['code_verifier'] = verifier
	session['oauth_state'] = state
	session['region_key'] = region_key
	session['region_host'] = region['host']
	
	auth_params = {
		'client_id': CLIENT_ID,
		'response_type': 'code',
		'redirect_uri': REDIRECT_URI,
		'code_challenge': generate_code_challenge(verifier),
		'code_challenge_method': 'S256',
		'state': state
	}
	return redirect(f"https://login.{region['host']}/oauth/authorize?{urllib.parse.urlencode(auth_params)}")

@app.route('/callback')
def callback():
	if request.args.get('error'):
		flash(f"Login failed: {request.args.get('error')}", 'danger')
		return redirect(url_for('index'))
	
	code = request.args.get('code')
	if not code:
		return redirect(url_for('index'))
	
	token, error = exchange_code_for_token(code, session.get('region_host'), session.get('code_verifier'))
	if error:
		flash(f'Token exchange failed: {error}', 'danger')
		return redirect(url_for('index'))
	
	session['access_token'] = token
	user = get_user_info(token, session.get('region_host'))
	if user:
		session['user_info'] = user
	
	return redirect(url_for('dashboard'))

@app.route('/dashboard')
@login_required
def dashboard():
	token = session.get('access_token')
	host = session.get('region_host')
	
	voicemails, error = get_all_voicemails(token, host)
	
	if error:
		flash(f"Error fetching voicemails: {error}", 'warning')
		voicemails = []
		
	formatted = [format_voicemail(vm) for vm in voicemails]
	preview = formatted[:20]
	
	total_sec = sum(vm.get('audioRecordingDurationSeconds', 0) or 0 for vm in voicemails)
	
	return render_template('dashboard.html',
						 user_info=session.get('user_info'),
						 region=REGIONS.get(session.get('region_key')),
						 voicemails=preview,
						 voicemail_count=len(formatted),
						 preview_count=len(preview),
						 total_duration_minutes=round(total_sec/60, 1),
						 enable_downloads=ENABLE_DOWNLOADS)

@app.route('/download/<message_id>')
@login_required
def download_single(message_id):
	if not ENABLE_DOWNLOADS:
		flash('Download functionality is currently disabled.', 'warning')
		return redirect(url_for('dashboard'))
	
	access_token = session.get('access_token')
	region_host = session.get('region_host')
	
	meta_url = f"https://api.{region_host}/api/v2/voicemail/messages/{message_id}"
	meta_data, meta_error = make_api_request(meta_url, access_token)
	
	if meta_error or not meta_data:
		filename = f"voicemail_{message_id}.wav"
	else:
		filename = format_filename(meta_data)
	
	media_bytes, error = download_voicemail_media(access_token, region_host, message_id)
	
	if error:
		flash(f'Download failed: {error}', 'danger')
		return redirect(url_for('dashboard'))
	
	return Response(
		media_bytes,
		mimetype='audio/wav',
		headers={'Content-Disposition': f'attachment; filename="{filename}"'}
	)

@app.route('/download')
@login_required
def download_page():
	if not ENABLE_DOWNLOADS:
		flash('Download functionality is currently disabled.', 'warning')
		return redirect(url_for('dashboard'))
	voicemails, _ = get_all_voicemails(session.get('access_token'), session.get('region_host'))
	if not voicemails:
		voicemails = []
	formatted = [format_voicemail(vm) for vm in voicemails]
	return render_template('download.html',
						 user_info=session.get('user_info'),
						 region=REGIONS.get(session.get('region_key')),
						 voicemails=formatted,
						 voicemail_count=len(formatted),
						 batch_size=DOWNLOAD_BATCH_SIZE)

@app.route('/download-prepare', methods=['POST'])
@login_required
def download_prepare():
	schedule_keepalive()
	data = request.get_json()
	ids = set(data.get('voicemail_ids', []))
	
	all_vms, _ = get_all_voicemails(session.get('access_token'), session.get('region_host'))
	selected = [vm for vm in all_vms if vm['id'] in ids]
	
	if not selected:
		return jsonify({'success': False, 'error': 'No matching voicemails'}), 400
	
	user_name = session.get('user_info', {}).get('name', 'User').replace(' ', '_')
	manifest_id = str(uuid.uuid4())
	num_batches = (len(selected) + BATCH_DOWNLOAD_SIZE - 1) // BATCH_DOWNLOAD_SIZE
	
	batches_info = []
	
	for i in range(num_batches):
		batch_vms = selected[i*BATCH_DOWNLOAD_SIZE : (i+1)*BATCH_DOWNLOAD_SIZE]
		batch_id = str(uuid.uuid4())
		
		with batch_lock:
			prepared_batches[batch_id] = {
				'status': 'queued',
				'created': time.time(),
				'batch_num': i+1,
				'total_batches': num_batches,
				'total': len(batch_vms),
				'manifest_id': manifest_id,
				'filename': f"voicemails_batch{i+1}_{user_name}.zip"
			}
			batches_info.append(prepared_batches[batch_id])
			
		batch_queue.put((batch_id, batch_vms, session.get('access_token'), session.get('region_host'), user_name, i+1, num_batches))
	
	start_batch_worker()
	
	return jsonify({'success': True, 'manifest_id': manifest_id, 'total_voicemails': len(selected)})

@app.route('/download-manifest')
@login_required
def download_manifest():
	mid = request.args.get('manifest_id')
	batches = [
		{'id': k, **v} for k,v in prepared_batches.items() if v.get('manifest_id') == mid
	]
	batches.sort(key=lambda x: x['batch_num'])
	if not batches:
		return redirect(url_for('download_page'))
	
	return render_template('download_manifest.html',
						 user_info=session.get('user_info'),
						 region=REGIONS.get(session.get('region_key')),
						 manifest_id=mid,
						 batches=batches,
						 total_voicemails=sum(b['total'] for b in batches),
						 expiry_minutes=BATCH_EXPIRY_SECONDS // 60)

@app.route('/download-batch/<batch_id>')
@login_required
def download_batch_file(batch_id):
	b = prepared_batches.get(batch_id)
	if not b or not b.get('zip_path') or not os.path.exists(b['zip_path']):
		flash('Download expired or not found', 'danger')
		return redirect(url_for('download_page'))
	return send_file(b['zip_path'], as_attachment=True, download_name=b['filename'])

@app.route('/api/batch-status')
@login_required
def api_batch_status():
	mid = request.args.get('manifest_id')
	batches = [{'id': k, **v} for k,v in prepared_batches.items() if v.get('manifest_id') == mid]
	return jsonify({
		'batches': batches,
		'all_ready': all(b['status'] == 'ready' for b in batches),
		'any_failed': any(b['status'] == 'failed' for b in batches)
	})

@app.route('/forward')
@login_required
def forward_page():
	voicemails, _ = get_all_voicemails(session.get('access_token'), session.get('region_host'))
	formatted = [format_voicemail(vm) for vm in (voicemails or [])]
	return render_template('forward.html',
						 user_info=session.get('user_info'),
						 region=REGIONS.get(session.get('region_key')),
						 voicemails=formatted,
						 voicemail_count=len(formatted),
						 batch_size=BATCH_SIZE)

@app.route('/delete')
@login_required
def delete_page():
	voicemails, _ = get_all_voicemails(session.get('access_token'), session.get('region_host'))
	formatted = [format_voicemail(vm) for vm in (voicemails or [])]
	return render_template('delete.html',
						 user_info=session.get('user_info'),
						 region=REGIONS.get(session.get('region_key')),
						 voicemails=formatted,
						 voicemail_count=len(formatted),
						 batch_size=BATCH_SIZE)

@app.route('/api/forward', methods=['POST'])
@login_required
def api_forward():
	schedule_keepalive()
	data = request.get_json()
	ids = data.get('voicemail_ids', [])
	res = process_voicemails_in_batches(
		session.get('access_token'), session.get('region_host'), ids, 'forward',
		target_id=data.get('target_id'), target_type=data.get('target_type')
	)
	return jsonify(res)

@app.route('/api/delete', methods=['POST'])
@login_required
def api_delete():
	schedule_keepalive()
	data = request.get_json()
	ids = data.get('voicemail_ids', [])
	res = process_voicemails_in_batches(
		session.get('access_token'), session.get('region_host'), ids, 'delete'
	)
	return jsonify(res)

@app.route('/api/search/users')
@login_required
def api_user_search():
	res, _ = search_users(session.get('access_token'), session.get('region_host'), request.args.get('q',''))
	return jsonify({'users': [{'id': u['id'], 'name': u['name'], 'email': u.get('email')} for u in res]})

@app.route('/api/search/groups')
@login_required
def api_group_search():
	res, _ = search_groups(session.get('access_token'), session.get('region_host'), request.args.get('q',''))
	return jsonify({'groups': [{'id': g['id'], 'name': g['name'], 'memberCount': g.get('memberCount',0)} for g in res]})

@app.route('/logout')
def logout():
	session.clear()
	return redirect(url_for('index'))

@app.route('/health')
def health():
	return jsonify({'status': 'healthy', 'version': 'v15-dates-forwarding'})

@app.route('/documentation')
def documentation():
	return render_template('documentation.html')

# ============================================================================
# ERROR HANDLERS & FILTERS
# ============================================================================

@app.errorhandler(404)
def not_found(e):
	return render_template('error.html', error_code=404, error_message='Page not found'), 404

@app.errorhandler(500)
def server_error(e):
	return render_template('error.html', error_code=500, error_message='Internal server error'), 500

@app.template_filter('datetime')
def datetime_filter(value):
	return format_datetime(value)

@app.template_filter('duration')
def duration_filter(value):
	return format_duration(value)

@app.template_filter('datetime_short')
def datetime_short_filter(value):
	return format_datetime_short(value)

def schedule_keepalive():
	def ping():
		time.sleep(840)
		try:
			urllib.request.urlopen(request.host_url + "health", timeout=5)
		except:
			pass
	threading.Thread(target=ping, daemon=True).start()

if __name__ == '__main__':
	app.run(debug=True, host='127.0.0.1', port=5000)