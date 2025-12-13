#!/usr/bin/env python3
"""
Genesys Cloud Voicemail Manager - v20
==========================================
CHANGES FROM v19:
- Refactored forward to 3-phase approach:
  Phase 1: Prepare dictionary (conversation_id -> createdDate)
  Phase 2: Populate datatable row by row (0.25s delay)
  Phase 3: Forward messages (0.5s delay)
- Removed bulk import job approach
- Removed embedded timestamp in callerAddress
- Any datatable error aborts entire operation
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
import time
import threading
from datetime import datetime
from functools import wraps
from collections import defaultdict

from flask import (
	Flask, render_template, request, redirect, url_for,
	session, flash, send_file, jsonify, Response
)

import io
import csv

# ============================================================================
# FLASK APP CONFIGURATION
# ============================================================================

app = Flask(__name__)

def start_keepalive_loop():
	"""Ping /health every 12 minutes to prevent spin down"""
	def keepalive_worker():
		while True:
			time.sleep(720)  # 12 minutes
			try:
				url = os.environ.get('RENDER_EXTERNAL_URL', 'http://127.0.0.1:5000')
				urllib.request.urlopen(f"{url}/health", timeout=10)
				app.logger.info("Keepalive ping sent")
			except Exception as e:
				app.logger.warning(f"Keepalive failed: {e}")
	
	if os.environ.get('RENDER_EXTERNAL_URL'):  # Only on Render
		threading.Thread(target=keepalive_worker, daemon=True).start()
		app.logger.info("Keepalive loop started")

start_keepalive_loop()

app.secret_key = os.environ.get('FLASK_SECRET_KEY', secrets.token_hex(32))

app.config['SESSION_TYPE'] = 'filesystem'
app.config['PERMANENT_SESSION_LIFETIME'] = 3600

# ============================================================================
# GENESYS CONFIGURATION
# ============================================================================

CLIENT_ID = os.environ.get('GENESYS_CLIENT_ID', '')
DATATABLE_ID = os.environ.get('GENESYS_DATATABLE_ID', '') 

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
API_DELAY_GET = 0.25
API_DELAY_WRITE = 0.5
API_DELAY_DATATABLE = 0.25  # 240/min, safe buffer under 300/min limit
API_DELAY_GROUP_FETCH = 0.5

# Batch settings
BATCH_SIZE = 20
DOWNLOAD_BATCH_SIZE = 10
SUPER_BATCH_SIZE = 5
SUPER_BATCH_DELAY = 2.0

# State management
progress_data = {}
user_operation_locks = defaultdict(threading.Lock)

# ============================================================================
# DATA TABLE FUNCTIONS
# ============================================================================

def save_original_date(access_token, region_host, datatable_id, conversation_id, original_date):
	"""Write original date to data table using POST (Create). Returns (success, error_message)."""
	if not datatable_id or not conversation_id or not original_date:
		return False, "Missing required parameters"
	
	url = f"https://api.{region_host}/api/v2/flows/datatables/{datatable_id}/rows"
	
	data = {
		"key": conversation_id, 
		"originalCreatedDate": original_date
	}
	
	response, error = make_api_request(url, access_token, method='POST', data=data)
	
	if error:
		return False, error
		
	return True, None

def get_original_date(access_token, region_host, datatable_id, conversation_id):
	"""Read original date from data table"""
	if not datatable_id or not conversation_id:
		return None
		
	url = f"https://api.{region_host}/api/v2/flows/datatables/{datatable_id}/rows/{conversation_id}?showbrief=false"
	data, error = make_api_request(url, access_token)
	
	if error:
		return None
		
	return data.get('originalCreatedDate')

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

# ============================================================================
# MAILBOX SELECTION HELPERS
# ============================================================================

def get_current_mailbox():
	"""Get the currently selected mailbox from session."""
	if 'current_mailbox' in session:
		return session['current_mailbox']

	user_info = session.get('user_info', {})
	return {
		'type': 'user',
		'id': user_info.get('id'),
		'name': user_info.get('name', 'My Voicemails')
	}

def set_current_mailbox(mailbox_type, mailbox_id, mailbox_name):
	"""Set the current mailbox in session."""
	session['current_mailbox'] = {
		'type': mailbox_type,
		'id': mailbox_id,
		'name': mailbox_name
	}
	app.logger.info(f"Switched to mailbox: {mailbox_type} - {mailbox_name} ({mailbox_id})")

def get_cached_user_groups():
	"""Get cached user groups from session."""
	return session.get('user_groups', [])

def initialize_default_mailbox():
	"""Initialize the mailbox to the user's own mailbox if not already set."""
	if 'current_mailbox' not in session:
		user_info = session.get('user_info', {})
		set_current_mailbox('user', user_info.get('id'), user_info.get('name', 'My Voicemails'))

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

def get_all_voicemails(access_token, region_host, user_id=None, mailbox_type='user', mailbox_id=None):
	"""Fetch voicemails for either a user or group mailbox."""
	all_entities = []

	if mailbox_type == 'user':
		if not mailbox_id:
			if not user_id:
				user_info = get_user_info(access_token, region_host)
				if not user_info:
					return None, "Could not get user info"
				mailbox_id = user_info.get('id')
			else:
				mailbox_id = user_id

		url = f"https://api.{region_host}/api/v2/voicemail/search"
		page_number = 1
		page_size = API_PAGE_SIZE

		search_body = {
			"pageSize": page_size,
			"pageNumber": page_number,
			"query": [
				{"fields": ["owner"], "type": "EXACT", "value": "user"},
				{"fields": ["ownerId"], "type": "EXACT", "value": mailbox_id}
			]
		}

		app.logger.info(f"Fetching voicemails for user {mailbox_id} via POST search...")
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

	elif mailbox_type == 'group':
		if not mailbox_id:
			return None, "Group ID required for group mailbox"

		url = f"https://api.{region_host}/api/v2/voicemail/search"
		page_number = 1
		page_size = API_PAGE_SIZE

		search_body = {
			"pageSize": page_size,
			"pageNumber": page_number,
			"query": [
				{"fields": ["owner"], "type": "EXACT", "value": "group"},
				{"fields": ["ownerId"], "type": "EXACT", "value": mailbox_id}
			]
		}

		app.logger.info(f"Fetching voicemails for group {mailbox_id} via POST search...")
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
				app.logger.info(f"API reports {total_expected} total group voicemails")

			page_count = data.get('pageCount', 0)
			if page_number >= page_count or not results:
				break

			page_number += 1
			time.sleep(API_DELAY_GET)

	else:
		return None, f"Invalid mailbox_type: {mailbox_type}"

	# Deduplicate, filter deleted, sort
	unique_map = {v['id']: v for v in all_entities}
	unique_entities = list(unique_map.values())
	active_voicemails = [v for v in unique_entities if not v.get('deleted', False)]
	active_voicemails.sort(key=lambda vm: vm.get('createdDate', ''), reverse=True)

	app.logger.info(
		f"Fetch Complete ({mailbox_type}): {len(all_entities)} raw, "
		f"{len(unique_entities)} unique, {len(active_voicemails)} active."
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

def get_user_groups(access_token, region_host):
	"""Fetch all groups that the current user is a member of."""
	url = f"https://api.{region_host}/api/v2/users/me?expand=groups"
	
	data, error = make_api_request(url, access_token)
	
	if error:
		app.logger.error(f"Error fetching user with groups: {error}")
		return [], None
	
	if not data:
		return [], None
	
	group_refs = data.get('groups', [])
	
	if not group_refs:
		return [], None
	
	detailed_groups = []
	for group_ref in group_refs:
		group_id = group_ref.get('id')
		if not group_id:
			continue
			
		group_url = f"https://api.{region_host}/api/v2/groups/{group_id}"
		group_data, group_error = make_api_request(group_url, access_token)
		
		if group_error:
			app.logger.warning(f"Error fetching group {group_id}: {group_error}")
			continue
			
		if group_data:
			detailed_groups.append({
				'id': group_data.get('id'),
				'name': group_data.get('name', 'Unknown Group'),
				'memberCount': group_data.get('memberCount', 0)
			})

		time.sleep(API_DELAY_GROUP_FETCH)
	
	app.logger.info(f"Fetched details for {len(detailed_groups)} groups")
	return detailed_groups, None

# ============================================================================
# BATCH PROCESSOR - 3 PHASE FORWARD
# ============================================================================

def process_voicemails_in_batches(access_token, region_host, voicemails_data, operation, 
								   target_id=None, target_type='user', progress_id=None):
	"""
	Process voicemails for forward or delete operations.
	
	For forward: 3-phase approach
	  Phase 1: Prepare dictionary (conversation_id -> createdDate)
	  Phase 2: Populate datatable row by row
	  Phase 3: Forward messages
	
	For delete: Simple sequential delete
	"""
	app.logger.info(f"=== BATCH {operation.upper()} START ===")
	app.logger.info(f"Input VMs: {len(voicemails_data)}")
	
	# Deduplicate by ID
	seen = set()
	unique_vms = []
	for vm in voicemails_data:
		if vm['id'] not in seen:
			seen.add(vm['id'])
			unique_vms.append(vm)
	
	if len(unique_vms) != len(voicemails_data):
		app.logger.warning(f"Removed {len(voicemails_data) - len(unique_vms)} duplicates")
	
	total = len(unique_vms)
	results = {'success': 0, 'failed': 0, 'errors': [], 'total': total, 'processed': 0}
	
	if progress_id:
		progress_data[progress_id] = {'processed': 0, 'total': total, 'success': 0, 'failed': 0, 'status': 'processing'}
	
	if operation == 'forward':
		# ========== PHASE 1: Prepare dictionary ==========
		app.logger.info("Phase 1: Preparing conversation_id -> createdDate dictionary...")
		
		date_dict = {}
		for vm in unique_vms:
			conversation_id = vm.get('conversation', {}).get('id')
			created_date = vm.get('createdDate')
			
			if conversation_id and created_date:
				date_dict[conversation_id] = created_date
		
		app.logger.info(f"Phase 1 complete: {len(date_dict)} entries prepared")
		
		# ========== PHASE 2: Populate datatable ==========
		if DATATABLE_ID and date_dict:
			app.logger.info(f"Phase 2: Populating datatable with {len(date_dict)} entries...")
			
			for i, (conv_id, created_date) in enumerate(date_dict.items()):
				success, error = save_original_date(access_token, region_host, DATATABLE_ID, conv_id, created_date)
				
				if not success:
					if "not unique" in str(error).lower() or "duplicate" in str(error).lower():
						app.logger.debug(f"Datatable row exists for {conv_id[:8]}, skipping")
					else:
						error_msg = f"Datatable write failed for {conv_id[:8]}: {error}"
						app.logger.error(error_msg)
						results['failed'] = total
						results['errors'].append(error_msg)
						
						if progress_id:
							progress_data[progress_id]['status'] = 'failed'
							progress_data[progress_id]['failed'] = total
						
						app.logger.info("=== BATCH FORWARD ABORTED (Phase 2 failure) ===")
						return results
				
				if (i + 1) % 50 == 0:
					app.logger.info(f"Phase 2 progress: {i + 1}/{len(date_dict)} datatable entries written")
				
				time.sleep(API_DELAY_DATATABLE)
			
			app.logger.info(f"Phase 2 complete: All {len(date_dict)} datatable entries written")
		else:
			app.logger.info("Phase 2 skipped: No DATATABLE_ID configured or no entries to write")
		
		# ========== PHASE 3: Forward messages ==========
		app.logger.info(f"Phase 3: Forwarding {total} messages...")
		
		for i, vm in enumerate(unique_vms):
			vm_id = vm['id']
			
			try:
				url = f"https://api.{region_host}/api/v2/voicemail/messages"
				body = {"voicemailMessageId": vm_id}
				
				if target_type == 'group':
					body["groupId"] = target_id
				else:
					body["userId"] = target_id
				
				response_data, err = make_api_request(url, access_token, 'POST', body)
				
				if err:
					results['failed'] += 1
					results['errors'].append(f"VM {vm_id[:8]}: {err}")
					app.logger.error(f"Forward FAIL: {vm_id[:8]} - {err}")
				else:
					results['success'] += 1
					new_id = response_data.get('id', 'unknown') if response_data else 'unknown'
					app.logger.debug(f"Forward OK: {vm_id[:8]} -> {new_id[:8] if new_id != 'unknown' else 'unknown'}")
					
			except Exception as e:
				results['failed'] += 1
				results['errors'].append(f"VM {vm_id[:8]}: {str(e)}")
				app.logger.exception(f"Exception forwarding {vm_id[:8]}: {e}")
			
			results['processed'] += 1
			
			if progress_id:
				progress_data[progress_id].update({
					'processed': results['processed'],
					'success': results['success'],
					'failed': results['failed']
				})
			
			if (i + 1) % 50 == 0:
				app.logger.info(f"Phase 3 progress: {i + 1}/{total} - Success: {results['success']}, Failed: {results['failed']}")
			
			time.sleep(API_DELAY_WRITE)
	
	elif operation == 'delete':
		# Simple sequential delete
		for i, vm in enumerate(unique_vms):
			vm_id = vm['id']
			
			try:
				url = f"https://api.{region_host}/api/v2/voicemail/messages/{vm_id}"
				_, err = make_api_request(url, access_token, 'DELETE')
				
				if err:
					results['failed'] += 1
					results['errors'].append(f"VM {vm_id[:8]}: {err}")
					app.logger.error(f"Delete FAIL: {vm_id[:8]} - {err}")
				else:
					results['success'] += 1
					
			except Exception as e:
				results['failed'] += 1
				results['errors'].append(f"VM {vm_id[:8]}: {str(e)}")
				app.logger.exception(f"Exception deleting {vm_id[:8]}: {e}")
			
			results['processed'] += 1
			
			if progress_id:
				progress_data[progress_id].update({
					'processed': results['processed'],
					'success': results['success'],
					'failed': results['failed']
				})
			
			if (i + 1) % 50 == 0:
				app.logger.info(f"Delete progress: {i + 1}/{total} - Success: {results['success']}, Failed: {results['failed']}")
			
			time.sleep(API_DELAY_WRITE)
	
	if progress_id:
		progress_data[progress_id]['status'] = 'complete'
	
	app.logger.info(f"=== BATCH {operation.upper()} END === Total: {total}, Success: {results['success']}, Failed: {results['failed']}")
	
	return results

# ============================================================================
# HELPERS
# ============================================================================

def format_voicemail(vm, access_token, region_host, load_original_dates=False):
	"""Format voicemail with full date and forwarding info"""
	created = vm.get('createdDate', '')
	modified = vm.get('modifiedDate', '')
	
	caller_name = vm.get('callerName', '')
	caller_address = vm.get('callerAddress', '')
	
	# Parse embedded original timestamp (legacy support)
	embedded_date = None
	source_field = caller_name if caller_name else caller_address

	if source_field and source_field.startswith('[') and ']' in source_field:
		try:
			end_bracket = source_field.index(']')
			embedded_date = source_field[1:end_bracket].strip()
			source_field = source_field[end_bracket + 1:].strip()
		except:
			pass

	original_caller = caller_name if caller_name else source_field if source_field else 'Unknown'
	
	copied_from = vm.get('copiedFrom')
	is_forwarded = copied_from is not None
	forwarded_by = None
	original_date = None
	
	conversation_id = vm.get('conversation', {}).get('id')
	
	if is_forwarded:
		copied_from_user = copied_from.get('user', {})
		forwarded_by = copied_from_user.get('name', 'Unknown')
		
		# Fallback chain: embedded -> copiedFrom.date -> datatable
		original_date = embedded_date if embedded_date else copied_from.get('date')
		
		if load_original_dates and not original_date and conversation_id and DATATABLE_ID:
			original_date = get_original_date(access_token, region_host, DATATABLE_ID, conversation_id)
			time.sleep(API_DELAY_DATATABLE)

	copied_to = vm.get('copiedTo', [])
	forwarded_to_name = None
	forwarded_status_date = None

	if copied_to:
		copied_to.sort(key=lambda x: x.get('date', ''), reverse=True)
		latest_forward = copied_to[0]

		if latest_forward.get('group'):
			forwarded_to_name = latest_forward['group'].get('name')
		elif latest_forward.get('user'):
			forwarded_to_name = latest_forward['user'].get('name')
		
		raw_fw_date = latest_forward.get('date')
		if raw_fw_date:
			forwarded_status_date = format_datetime(raw_fw_date)

	return {
		'id': vm.get('id'),
		'id_short': vm.get('id', '')[:8],
		'original_caller': original_caller,
		'caller_name': caller_name,
		'created_date': format_datetime(created),
		'created_date_raw': created,
		'original_date': format_datetime(original_date) if original_date else None,
		'original_date_raw': original_date,
		'modified_date': format_datetime(modified) if modified else None,
		'duration': format_duration(vm.get('audioRecordingDurationSeconds')),
		'duration_seconds': vm.get('audioRecordingDurationSeconds'),
		'read': vm.get('read', False),
		'is_forwarded': is_forwarded,
		'forwarded_by': forwarded_by,
		'forwarded_to': forwarded_to_name,
		'forwarded_status_date': forwarded_status_date,
		'filename': format_filename(vm),
		'conversation_id': conversation_id,
	}

def format_filename(voicemail):
	"""Generate filename with both created and modified dates if different"""
	msg_id = voicemail.get('id', 'unknown')
	caller_name = voicemail.get('callerName', 'Unknown')
	created_date = voicemail.get('createdDate', '')
	modified_date = voicemail.get('modifiedDate', '')
	
	created_str = 'unknown'
	if created_date:
		try:
			dt = datetime.fromisoformat(created_date.replace('Z', '+00:00'))
			created_str = dt.strftime('%Y%m%d_%H%M%S')
		except:
			pass
	
	modified_str = None
	if modified_date and modified_date != created_date:
		try:
			dt = datetime.fromisoformat(modified_date.replace('Z', '+00:00'))
			modified_str = dt.strftime('%Y%m%d_%H%M%S')
		except:
			pass
	
	safe_caller = ''.join(c if c.isalnum() or c in ' -_' else '_' for c in str(caller_name))[:30]
	
	if modified_str and modified_str != created_str:
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
	except Exception as e:
		app.logger.error(f"Cleanup error: {e}")

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

		user_groups, groups_error = get_user_groups(token, session.get('region_host'))
		if groups_error:
			app.logger.warning(f"Error fetching groups at login: {groups_error}")
			session['user_groups'] = []
		else:
			session['user_groups'] = user_groups if user_groups else []
			app.logger.info(f"Cached {len(session['user_groups'])} groups at login")

	return redirect(url_for('dashboard'))

@app.route('/dashboard')
@login_required
def dashboard():
	token = session.get('access_token')
	host = session.get('region_host')

	initialize_default_mailbox()
	user_groups = get_cached_user_groups()
	current_mailbox = get_current_mailbox()

	voicemails, error = get_all_voicemails(
		token, host,
		mailbox_type=current_mailbox['type'],
		mailbox_id=current_mailbox['id']
	)

	if error:
		flash(f"Error fetching voicemails: {error}", 'warning')
		voicemails = []

	formatted = [format_voicemail(vm, token, host, load_original_dates=False) for vm in voicemails]
	total_sec = sum(vm.get('audioRecordingDurationSeconds', 0) or 0 for vm in voicemails)

	# For group mailboxes, show all voicemails; for user mailboxes, show preview of 20
	if current_mailbox['type'] == 'group':
		display_voicemails = formatted
		is_preview = False
	else:
		display_voicemails = formatted[:20]
		is_preview = True

	return render_template('dashboard.html',
						 user_info=session.get('user_info'),
						 region=REGIONS.get(session.get('region_key')),
						 voicemails=display_voicemails,
						 voicemail_count=len(formatted),
						 preview_count=len(display_voicemails),
						 is_preview=is_preview,
						 total_duration_minutes=round(total_sec/60, 1),
						 enable_downloads=ENABLE_DOWNLOADS,
						 user_groups=user_groups,
						 current_mailbox=current_mailbox)

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

	initialize_default_mailbox()
	user_groups = get_cached_user_groups()
	current_mailbox = get_current_mailbox()

	voicemails, _ = get_all_voicemails(
		session.get('access_token'),
		session.get('region_host'),
		mailbox_type=current_mailbox['type'],
		mailbox_id=current_mailbox['id']
	)
	if not voicemails:
		voicemails = []
	formatted = [format_voicemail(vm, session.get('access_token'), session.get('region_host'), load_original_dates=False) for vm in voicemails]
	return render_template('download.html',
						 user_info=session.get('user_info'),
						 region=REGIONS.get(session.get('region_key')),
						 voicemails=formatted,
						 voicemail_count=len(formatted),
						 batch_size=DOWNLOAD_BATCH_SIZE,
						 user_groups=user_groups,
						 current_mailbox=current_mailbox)

@app.route('/forward')
@login_required
def forward_page():
	initialize_default_mailbox()
	user_groups = get_cached_user_groups()
	current_mailbox = get_current_mailbox()

	voicemails, _ = get_all_voicemails(
		session.get('access_token'),
		session.get('region_host'),
		mailbox_type=current_mailbox['type'],
		mailbox_id=current_mailbox['id']
	)
	formatted = [format_voicemail(vm, session.get('access_token'), session.get('region_host'), load_original_dates=False) for vm in (voicemails or [])]
	return render_template('forward.html',
						 user_info=session.get('user_info'),
						 region=REGIONS.get(session.get('region_key')),
						 voicemails=formatted,
						 voicemail_count=len(formatted),
						 batch_size=BATCH_SIZE,
						 user_groups=user_groups,
						 current_mailbox=current_mailbox)

@app.route('/delete')
@login_required
def delete_page():
	initialize_default_mailbox()
	user_groups = get_cached_user_groups()
	current_mailbox = get_current_mailbox()

	voicemails, _ = get_all_voicemails(
		session.get('access_token'),
		session.get('region_host'),
		mailbox_type=current_mailbox['type'],
		mailbox_id=current_mailbox['id']
	)
	formatted = [format_voicemail(vm, session.get('access_token'), session.get('region_host'), load_original_dates=False) for vm in (voicemails or [])]
	return render_template('delete.html',
						 user_info=session.get('user_info'),
						 region=REGIONS.get(session.get('region_key')),
						 voicemails=formatted,
						 voicemail_count=len(formatted),
						 batch_size=BATCH_SIZE,
						 user_groups=user_groups,
						 current_mailbox=current_mailbox)

@app.route('/api/forward', methods=['POST'])
@login_required
def api_forward():
	data = request.get_json()
	ids = data.get('voicemail_ids', [])
	
	all_vms, _ = get_all_voicemails(
		session.get('access_token'),
		session.get('region_host'),
		mailbox_type=get_current_mailbox()['type'],
		mailbox_id=get_current_mailbox()['id']
	)
	
	selected_vms = [vm for vm in (all_vms or []) if vm['id'] in ids]
	
	res = process_voicemails_in_batches(
		session.get('access_token'), session.get('region_host'), selected_vms, 'forward',
		target_id=data.get('target_id'), target_type=data.get('target_type')
	)
	return jsonify(res)

@app.route('/api/delete', methods=['POST'])
@login_required
def api_delete():
	data = request.get_json()
	ids = data.get('voicemail_ids', [])
	
	vms_data = [{'id': vm_id} for vm_id in ids]
	
	res = process_voicemails_in_batches(
		session.get('access_token'), session.get('region_host'), vms_data, 'delete'
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

@app.route('/api/switch-mailbox', methods=['POST'])
@login_required
def api_switch_mailbox():
	"""API endpoint to switch the currently selected mailbox"""
	data = request.get_json()
	mailbox_type = data.get('type')
	mailbox_id = data.get('id')
	mailbox_name = data.get('name')

	if not mailbox_type or mailbox_type not in ['user', 'group']:
		return jsonify({'success': False, 'error': 'Invalid mailbox type'}), 400

	if not mailbox_id or not mailbox_name:
		return jsonify({'success': False, 'error': 'Missing mailbox id or name'}), 400

	set_current_mailbox(mailbox_type, mailbox_id, mailbox_name)

	return jsonify({'success': True})

@app.route('/api/voicemail/<message_id>/media-url')
@login_required
def get_voicemail_media_url(message_id):
	"""Get fresh media URL for audio playback. Returns mediaFileUri (temporary pre-signed URL)."""
	access_token = session.get('access_token')
	region_host = session.get('region_host')

	url = f"https://api.{region_host}/api/v2/voicemail/messages/{message_id}/media"
	params = {'formatId': 'WAV'}
	url_with_params = f"{url}?{urllib.parse.urlencode(params)}"

	req = urllib.request.Request(url_with_params)
	req.add_header('Authorization', f'Bearer {access_token}')

	try:
		with urllib.request.urlopen(req, timeout=30) as response:
			content_type = response.headers.get('Content-Type', '')

			if 'application/json' in content_type:
				data = json.loads(response.read().decode())
				if 'mediaFileUri' in data:
					return jsonify({
						'success': True,
						'mediaUrl': data['mediaFileUri']
					})
				return jsonify({'success': False, 'error': 'No media URI in response'}), 400
			else:
				# API returned direct binary - this shouldn't happen with our request but handle it
				return jsonify({'success': False, 'error': 'Unexpected response format'}), 400

	except urllib.request.HTTPError as e:
		error_msg = f"HTTP {e.code}: {e.reason}"
		try:
			error_body = json.loads(e.read().decode())
			error_msg = error_body.get('message', error_msg)
		except:
			pass
		app.logger.error(f"Error getting media URL for {message_id}: {error_msg}")
		return jsonify({'success': False, 'error': error_msg}), e.code
	except Exception as e:
		app.logger.error(f"Error getting media URL for {message_id}: {str(e)}")
		return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/load-original-dates', methods=['POST'])
@login_required
def load_original_dates():
	"""Load original dates from data table for specific voicemails using bulk endpoint."""
	data = request.get_json()
	conversation_ids = data.get('conversation_ids', [])

	if not DATATABLE_ID:
		return jsonify({'success': False, 'error': 'Data table not configured'}), 400

	if not conversation_ids:
		return jsonify({'success': False, 'error': 'No conversation IDs provided'}), 400

	if len(conversation_ids) > 500:
		return jsonify({'success': False, 'error': 'Too many IDs per request (max 500)'}), 400

	# Rate limit buffer - add delay at start of request to prevent hitting API limits
	time.sleep(1.0)

	access_token = session.get('access_token')
	region_host = session.get('region_host')

	# Deduplicate conversation IDs to avoid duplicate lookups
	unique_conv_ids = set(conversation_ids)

	app.logger.info(f"Loading original dates for {len(unique_conv_ids)} unique conversations (from {len(conversation_ids)} total)...")

	# Fetch all rows from datatable using bulk endpoint with pagination
	all_rows = {}
	url = f"https://api.{region_host}/api/v2/flows/datatables/{DATATABLE_ID}/rows?showbrief=false&pageSize=500"
	page_count = 0

	while url:
		page_count += 1
		response_data, error = make_api_request(url, access_token)

		if error:
			app.logger.error(f"Error fetching datatable rows (page {page_count}): {error}")
			break

		if not response_data:
			break

		# Extract rows from response - entities contains the row data
		entities = response_data.get('entities', [])
		for row in entities:
			row_key = row.get('key')
			original_date = row.get('originalCreatedDate')
			if row_key and original_date:
				all_rows[row_key] = original_date

		app.logger.info(f"Fetched page {page_count}: {len(entities)} rows (total cached: {len(all_rows)})")

		# Check for next page
		next_uri = response_data.get('nextUri')
		if next_uri:
			# nextUri is a relative path, construct full URL
			url = f"https://api.{region_host}{next_uri}"
			time.sleep(API_DELAY_DATATABLE)
		else:
			url = None

	# Match requested conversation IDs with fetched data
	results = {}
	fetched = 0
	failed = 0

	for conv_id in unique_conv_ids:
		if conv_id in all_rows:
			results[conv_id] = all_rows[conv_id]
			fetched += 1
		else:
			failed += 1

	app.logger.info(f"Loaded {fetched} original dates, {failed} not found (from {len(all_rows)} total datatable rows in {page_count} pages)")

	return jsonify({
		'success': True,
		'dates': results,
		'fetched': fetched,
		'failed': failed,
		'total': len(unique_conv_ids)
	})

@app.route('/logout')
def logout():
	session.clear()
	return redirect(url_for('index'))

@app.route('/health')
def health():
	return jsonify({'status': 'healthy', 'version': 'v20-3phase-forward'})

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

if __name__ == '__main__':
	app.run(debug=True, host='127.0.0.1', port=5000)
