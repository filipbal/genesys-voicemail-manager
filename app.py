#!/usr/bin/env python3
"""
Genesys Cloud Voicemail Manager - v19
==========================================
CHANGES FROM v18:
- Fixed 429 rate limit issue by implementing lazy loading for original dates
- Added API_DELAY_DATATABLE constant for rate limiting
- format_voicemail() now has load_original_dates=False parameter
- Added /api/load-original-dates endpoint for voluntary date loading
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

import csv # For bulk import

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
API_DELAY_GET = 0.3
API_DELAY_WRITE = 0.5
API_DELAY_GROUP_FETCH = 0.5  # Delay between fetching individual group details
API_DELAY_DATATABLE = 0.3  # Delay between data table lookups (~200 req/min)

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
	"""Write original date to data table using POST (Create)"""
	# URL points to the collection, not the specific row
	url = f"https://api.{region_host}/api/v2/flows/datatables/{datatable_id}/rows"
	
	# Payload must use 'key' for the primary key
	data = {
		"key": conversation_id, 
		"originalCreatedDate": original_date
	}
	
	response, error = make_api_request(url, access_token, method='POST', data=data)
	
	if error:
		# Ignore 409 Conflict (Row already exists)
		if "409" in str(error) or "conflict" in str(error).lower():
			return True
		print(f"Data Table Error: {error}")
		
	return error is None

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
	"""
	Get the currently selected mailbox from session.
	Returns dict with keys: type ('user' or 'group'), id, name
	If not set, returns default user mailbox.
	"""
	if 'current_mailbox' in session:
		return session['current_mailbox']

	# Default to user's own mailbox
	user_info = session.get('user_info', {})
	return {
		'type': 'user',
		'id': user_info.get('id'),
		'name': user_info.get('name', 'My Voicemails')
	}

def set_current_mailbox(mailbox_type, mailbox_id, mailbox_name):
	"""
	Set the current mailbox in session.

	Args:
		mailbox_type: 'user' or 'group'
		mailbox_id: ID of the user or group
		mailbox_name: Display name for the mailbox
	"""
	session['current_mailbox'] = {
		'type': mailbox_type,
		'id': mailbox_id,
		'name': mailbox_name
	}
	app.logger.info(f"Switched to mailbox: {mailbox_type} - {mailbox_name} ({mailbox_id})")

def get_cached_user_groups():
	"""
	Get cached user groups from session.
	Returns a list of groups that was fetched at login time.
	"""
	return session.get('user_groups', [])

def initialize_default_mailbox():
	"""
	Initialize the mailbox to the user's own mailbox if not already set.
	Should be called after login.
	"""
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
	"""
	Fetch voicemails for either a user or group mailbox.

	Args:
		access_token: OAuth bearer token
		region_host: API region host
		user_id: User ID (only used when mailbox_type='user' and mailbox_id is None)
		mailbox_type: Either 'user' or 'group'
		mailbox_id: ID of the user or group to fetch voicemails for
	"""
	all_entities = []

	# Determine the actual ID to use
	if mailbox_type == 'user':
		# For user mailbox, use mailbox_id if provided, otherwise use user_id or fetch current user
		if not mailbox_id:
			if not user_id:
				user_info = get_user_info(access_token, region_host)
				if not user_info:
					return None, "Could not get user info"
				mailbox_id = user_info.get('id')
			else:
				mailbox_id = user_id

		# Use POST search for user voicemails
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
					"value": mailbox_id
				}
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
		# Use POST search for group voicemails (same pattern as user voicemails)
		if not mailbox_id:
			return None, "Group ID required for group mailbox"

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
					"value": "group"
				},
				{
					"fields": ["ownerId"],
					"type": "EXACT",
					"value": mailbox_id
				}
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

	# Post-processing: deduplicate, filter deleted, and sort
	unique_map = {v['id']: v for v in all_entities}
	unique_entities = list(unique_map.values())

	active_voicemails = [v for v in unique_entities if not v.get('deleted', False)]

	active_voicemails.sort(key=lambda vm: vm.get('createdDate', ''), reverse=True)

	app.logger.info(
		f"Fetch Complete ({mailbox_type}): {len(all_entities)} raw, "
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

def get_user_groups(access_token, region_host):
	"""
	Fetch all groups that the current user is a member of.
	"""
	url = f"https://api.{region_host}/api/v2/users/me?expand=groups"
	
	data, error = make_api_request(url, access_token)
	
	if error:
		app.logger.error(f"Error fetching user with groups: {error}")
		return [], None
	
	if not data:
		return [], None
	
	# Groups only have id and selfUri - need to fetch details
	group_refs = data.get('groups', [])
	
	if not group_refs:
		return [], None
	
	# Fetch full details for each group
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

		time.sleep(API_DELAY_GROUP_FETCH)  # Rate limit protection
	
	app.logger.info(f"Fetched details for {len(detailed_groups)} groups")
	return detailed_groups, None

# ============================================================================
# OPTIMIZED BATCH PROCESSOR
# ============================================================================

def process_voicemails_in_batches(access_token, region_host, voicemail_ids, operation, 
								   target_id=None, target_type='user', progress_id=None):
	"""
	Process voicemails in batches with optimized 'Forward' logic.
	"""
	app.logger.info(f"=== BATCH {operation.upper()} START ===")
	app.logger.info(f"Input IDs: {len(voicemail_ids)}, Unique: {len(set(voicemail_ids))}")
	
	# Deduplicate input
	original_count = len(voicemail_ids)
	voicemail_ids = list(dict.fromkeys(voicemail_ids))
	total_ids = len(voicemail_ids)
	
	results = {'success': 0, 'failed': 0, 'errors': [], 'total': total_ids, 'processed': 0}
	
	# Initialize progress
	if progress_id:
		progress_data[progress_id] = {
			'processed': 0, 'total': total_ids, 
			'success': 0, 'failed': 0, 'status': 'processing'
		}

	# ---------------------------------------------------------
	# OPERATION: DELETE (Standard Sequential Processing)
	# ---------------------------------------------------------
	if operation == 'delete':
		for i, vm_id in enumerate(voicemail_ids):
			# Super batch sleep logic
			if i > 0 and i % (BATCH_SIZE * SUPER_BATCH_SIZE) == 0:
				time.sleep(SUPER_BATCH_DELAY)
			
			try:
				url = f"https://api.{region_host}/api/v2/voicemail/messages/{vm_id}"
				_, err = make_api_request(url, access_token, 'DELETE')
				
				if err is None:
					results['success'] += 1
				else:
					results['failed'] += 1
					results['errors'].append(f"VM {vm_id[:8]}: {err}")
			except Exception as e:
				results['failed'] += 1
				results['errors'].append(f"VM {vm_id[:8]}: {str(e)}")
			
			# Update Progress
			results['processed'] += 1
			if progress_id:
				progress_data[progress_id].update({
					'processed': results['processed'],
					'success': results['success'],
					'failed': results['failed']
				})
			
			time.sleep(API_DELAY_WRITE)

	# ---------------------------------------------------------
	# OPERATION: FORWARD (Optimized 3-Phase Processing)
	# ---------------------------------------------------------
	elif operation == 'forward':
		forward_payloads = []
		datatable_rows = []
		
		# PHASE 1: Fetch details and prepare data
		app.logger.info("Phase 1: Fetching voicemail details...")
		
		for i, vm_id in enumerate(voicemail_ids):
			# Super batch sleep logic for reads
			if i > 0 and i % (BATCH_SIZE * SUPER_BATCH_SIZE) == 0:
				time.sleep(SUPER_BATCH_DELAY)

			try:
				vm_url = f"https://api.{region_host}/api/v2/voicemail/messages/{vm_id}"
				vm_data, vm_err = make_api_request(vm_url, access_token, 'GET')
				
				if vm_err or not vm_data:
					results['failed'] += 1
					results['errors'].append(f"VM {vm_id[:8]}: Fetch failed - {vm_err}")
					# Count as processed since we won't try to forward it
					results['processed'] += 1
					if progress_id:
						progress_data[progress_id]['failed'] += 1
						progress_data[progress_id]['processed'] += 1
					continue

				# Extract Data
				original_created = vm_data.get('createdDate')
				conversation_id = vm_data.get('conversation', {}).get('id')

				# 1. Prepare Data Table Row
				if conversation_id and original_created:
					datatable_rows.append({
						'key': conversation_id,
						'originalCreatedDate': original_created
					})

				# 2. Prepare Forward Payload
				body = {"voicemailMessageId": vm_id}
				if target_type == 'group':
					body["groupId"] = target_id
				else:
					body["userId"] = target_id
				
				# Note: callerAddress embedding removed as Genesys API ignores custom values here
				
				forward_payloads.append(body)
				
				time.sleep(API_DELAY_GET)

			except Exception as e:
				results['failed'] += 1
				results['errors'].append(f"VM {vm_id[:8]}: Phase 1 Error - {str(e)}")
				results['processed'] += 1
				if progress_id:
					progress_data[progress_id]['failed'] += 1
					progress_data[progress_id]['processed'] += 1

		# PHASE 2: Bulk Data Table Update (Background Thread)
		# We fire this off asynchronously so forwarding can start immediately.
		if datatable_rows and DATATABLE_ID:
			app.logger.info(f"Phase 2: Bulk updating {len(datatable_rows)} dates...")
			threading.Thread(
				target=bulk_save_original_dates, 
				args=(access_token, region_host, DATATABLE_ID, datatable_rows),
				daemon=True
			).start()

		# PHASE 3: Execute Forwards
		app.logger.info(f"Phase 3: Executing {len(forward_payloads)} forwards...")
		
		for i, body in enumerate(forward_payloads):
			# Super batch sleep logic for writes
			if i > 0 and i % (BATCH_SIZE * SUPER_BATCH_SIZE) == 0:
				time.sleep(SUPER_BATCH_DELAY)
				
			try:
				url = f"https://api.{region_host}/api/v2/voicemail/messages"
				_, err = make_api_request(url, access_token, 'POST', body)
				
				if err is None:
					results['success'] += 1
				else:
					results['failed'] += 1
					results['errors'].append(f"Forward failed: {err}")

			except Exception as e:
				results['failed'] += 1
				results['errors'].append(f"Forward Exception: {str(e)}")
			
			# Update Progress (Only counting successful fetches that are now being forwarded)
			results['processed'] += 1
			if progress_id:
				progress_data[progress_id].update({
					'processed': results['processed'],
					'success': results['success'],
					'failed': results['failed']
				})
			
			time.sleep(API_DELAY_WRITE)

	# Final Status Update
	if progress_id:
		progress_data[progress_id]['status'] = 'complete'
	
	app.logger.info(f"=== BATCH {operation.upper()} END === Total: {total_ids}, Success: {results['success']}, Failed: {results['failed']}")
	
	return results

# ============================================================================
# HELPERS
# ============================================================================

def format_voicemail(vm, access_token, region_host, load_original_dates=False):
	"""Format voicemail with full date and forwarding info
	
	Args:
		vm: Voicemail data dict
		access_token: OAuth bearer token
		region_host: API region host
		load_original_dates: If True, fetch original dates from data table (slow, causes rate limits)
	"""
	created = vm.get('createdDate', '')
	modified = vm.get('modifiedDate', '')
	
	# Original caller info
	caller_name = vm.get('callerName', '')
	caller_address = vm.get('callerAddress', '')
	
	# Parse embedded original timestamp
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
	
	# Forwarding Info (Received Side)
	copied_from = vm.get('copiedFrom')
	is_forwarded = copied_from is not None
	forwarded_by = None
	original_date = None
	
	conversation_id = vm.get('conversation', {}).get('id')
	
	if is_forwarded:
		copied_from_user = copied_from.get('user', {})
		forwarded_by = copied_from_user.get('name', 'Unknown')
		
		# Fallback chain: embedded -> copiedFrom.date
		original_date = embedded_date if embedded_date else copied_from.get('date')
		
		# ONLY fetch data table if explicitly requested AND no fallback worked
		if load_original_dates and not original_date and conversation_id and DATATABLE_ID:
			original_date = get_original_date(access_token, region_host, DATATABLE_ID, conversation_id)
			time.sleep(API_DELAY_DATATABLE)  # Rate limit protection

	# Forwarding Info (Sent Side - LATEST ONLY)
	copied_to = vm.get('copiedTo', [])
	forwarded_to_name = None
	forwarded_status_date = None

	if copied_to:
		# 1. Sort by date descending (Newest first)
		copied_to.sort(key=lambda x: x.get('date', ''), reverse=True)
		latest_forward = copied_to[0]

		# 2. Extract Name
		if latest_forward.get('group'):
			forwarded_to_name = latest_forward['group'].get('name')
		elif latest_forward.get('user'):
			forwarded_to_name = latest_forward['user'].get('name')
		
		# 3. Extract Date
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
		# Updated Fields for User View
		'forwarded_to': forwarded_to_name,
		'forwarded_status_date': forwarded_status_date,
		'filename': format_filename(vm),
		'conversation_id': conversation_id,  # Add for frontend use
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
	except Exception as e:
		app.logger.error(f"Cleanup error: {e}")

# ============================================================================
# NEW HELPER: BULK IMPORT FOR DATA TABLES
# ============================================================================

def bulk_save_original_dates(access_token, region_host, datatable_id, rows_data):
	"""
	Performs a bulk import of original dates to the Genesys Data Table.
	
	Args:
		rows_data: List of dicts [{'key': conversation_id, 'originalCreatedDate': date}, ...]
	"""
	if not rows_data or not datatable_id:
		return

	try:
		# 1. Create CSV content in memory
		output = io.StringIO()
		writer = csv.writer(output)
		# Header must match Data Table field IDs
		writer.writerow(['key', 'originalCreatedDate']) 
		for row in rows_data:
			writer.writerow([row['key'], row['originalCreatedDate']])
		
		csv_content = output.getvalue()

		# 2. Create Import Job
		job_url = f"https://api.{region_host}/api/v2/flows/datatables/{datatable_id}/import/jobs"
		job_body = {"importMode": "Append"} 
		
		job_data, err = make_api_request(job_url, access_token, 'POST', job_body)
		
		if err or not job_data:
			print(f"Failed to create import job: {err}")
			return

		upload_uri = job_data.get('uploadURI')
		import_job_id = job_data.get('id')

		if not upload_uri:
			print("No upload URI returned for import job")
			return

		# 3. Upload CSV 
		# Genesys requires a specific PUT/POST to the signed URL. 
		# Standard urllib request for multipart/form-data or direct binary depending on provider.
		# For Genesys Data Tables, posting the raw CSV data to the uploadURI usually works.
		
		req = urllib.request.Request(upload_uri, data=csv_content.encode('utf-8'), method='POST')
		req.add_header('Content-Type', 'text/csv')
		req.add_header('Authorization', f'Bearer {access_token}')
		
		with urllib.request.urlopen(req) as response:
			if response.status not in [200, 201, 202, 204]:
				print(f"CSV Upload failed: {response.status}")
			else:
				print(f"Bulk import job {import_job_id} initiated for {len(rows_data)} rows.")

	except Exception as e:
		print(f"Exception during bulk data table import: {e}")

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

		# Cache user groups at login time to avoid repeated API calls
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

	# Initialize default mailbox if not set
	initialize_default_mailbox()

	# Get cached user groups from session
	user_groups = get_cached_user_groups()

	# Get current mailbox
	current_mailbox = get_current_mailbox()

	# Fetch voicemails for the selected mailbox
	voicemails, error = get_all_voicemails(
		token,
		host,
		mailbox_type=current_mailbox['type'],
		mailbox_id=current_mailbox['id']
	)

	if error:
		flash(f"Error fetching voicemails: {error}", 'warning')
		voicemails = []

	formatted = [format_voicemail(vm, token, host, load_original_dates=False) for vm in voicemails]
	preview = formatted[:20]

	total_sec = sum(vm.get('audioRecordingDurationSeconds', 0) or 0 for vm in voicemails)

	return render_template('dashboard.html',
						 user_info=session.get('user_info'),
						 region=REGIONS.get(session.get('region_key')),
						 voicemails=preview,
						 voicemail_count=len(formatted),
						 preview_count=len(preview),
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

	# Initialize default mailbox if not set
	initialize_default_mailbox()

	# Get cached user groups from session
	user_groups = get_cached_user_groups()

	# Get current mailbox
	current_mailbox = get_current_mailbox()

	# Fetch voicemails for the selected mailbox
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
	# Initialize default mailbox if not set
	initialize_default_mailbox()

	# Get cached user groups from session
	user_groups = get_cached_user_groups()

	# Get current mailbox
	current_mailbox = get_current_mailbox()

	# Fetch voicemails for the selected mailbox
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
	# Initialize default mailbox if not set
	initialize_default_mailbox()

	# Get cached user groups from session
	user_groups = get_cached_user_groups()

	# Get current mailbox
	current_mailbox = get_current_mailbox()

	# Fetch voicemails for the selected mailbox
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

@app.route('/api/switch-mailbox', methods=['POST'])
@login_required
def api_switch_mailbox():
	"""API endpoint to switch the currently selected mailbox"""
	data = request.get_json()
	mailbox_type = data.get('type')
	mailbox_id = data.get('id')
	mailbox_name = data.get('name')

	# Validate input
	if not mailbox_type or mailbox_type not in ['user', 'group']:
		return jsonify({'success': False, 'error': 'Invalid mailbox type'}), 400

	if not mailbox_id or not mailbox_name:
		return jsonify({'success': False, 'error': 'Missing mailbox id or name'}), 400

	# Set the mailbox in session
	set_current_mailbox(mailbox_type, mailbox_id, mailbox_name)

	return jsonify({'success': True})

@app.route('/api/load-original-dates', methods=['POST'])
@login_required
def load_original_dates():
	"""
	Load original dates from data table for specific voicemails.
	Intended for group mailbox view where users want accurate original timestamps.
	"""
	data = request.get_json()
	conversation_ids = data.get('conversation_ids', [])
	
	if not DATATABLE_ID:
		return jsonify({'success': False, 'error': 'Data table not configured'}), 400
	
	if not conversation_ids:
		return jsonify({'success': False, 'error': 'No conversation IDs provided'}), 400
	
	# Limit to prevent abuse
	if len(conversation_ids) > 2000:
		return jsonify({'success': False, 'error': 'Too many IDs (max 2000)'}), 400
	
	access_token = session.get('access_token')
	region_host = session.get('region_host')
	
	results = {}
	fetched = 0
	failed = 0
	
	app.logger.info(f"Loading original dates for {len(conversation_ids)} conversations...")
	
	for conv_id in conversation_ids:
		original_date = get_original_date(access_token, region_host, DATATABLE_ID, conv_id)
		
		if original_date:
			results[conv_id] = original_date
			fetched += 1
		else:
			failed += 1
		
		# Rate limiting
		time.sleep(API_DELAY_DATATABLE)
	
	app.logger.info(f"Loaded {fetched} original dates, {failed} not found")
	
	return jsonify({
		'success': True,
		'dates': results,
		'fetched': fetched,
		'failed': failed,
		'total': len(conversation_ids)
	})

@app.route('/logout')
def logout():
	session.clear()
	return redirect(url_for('index'))

@app.route('/health')
def health():
	return jsonify({'status': 'healthy', 'version': 'v19-lazy-loading'})

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
