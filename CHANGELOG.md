# Voicemail Manager - Performance & Rate Limiting Fixes (v4)

## v4 Changes

### 1. Batch Processing for Download All
The "Download All" function now uses the same batch processing system as forward/delete:
- Downloads in batches of 20 files
- 3-second delay between batches
- Super batch break every 5 batches
- Proper logging of progress

### 2. Sorting - Newest First
All voicemail lists are now sorted by date descending (newest first):
- Dashboard
- Forward page
- Delete page
- Download All (files in ZIP)

The sorting is applied in `get_all_voicemails_paginated()` so it affects all views consistently.

---

## v3 Changes (Previous)

### Root Cause: Gunicorn Worker Timeout
The errors were caused by Gunicorn's default 30-second worker timeout, not API rate limits.

### Solution
1. **`gunicorn.conf.py`** - Sets timeout to 600s (10 minutes)
2. **`render.yaml`** - Uses `gunicorn --config gunicorn.conf.py app:app`

---

## Current Configuration (v4)

```python
BATCH_SIZE = 20            # Operations per batch
BATCH_DELAY = 3.0          # Seconds between batches
OPERATION_DELAY = 0.2      # Seconds between operations
SUPER_BATCH_SIZE = 5       # Batches before long break
SUPER_BATCH_DELAY = 10.0   # Seconds for super batch break
```

## Time Estimates

| Operation | 50 items | 100 items | 500 items |
|-----------|----------|-----------|-----------|
| Forward | ~15s | ~30s | ~3 min |
| Delete | ~15s | ~30s | ~3 min |
| Download All | ~20s | ~45s | ~4 min |

## Files in v4

1. `gunicorn.conf.py` - Gunicorn config (10 min timeout)
2. `render.yaml` - Updated start command
3. `app.py` - Batch download + sorting
4. `templates/forward.html` - Progress UI
5. `templates/delete.html` - Progress UI
6. `CHANGELOG.md` - This file

## Updated Files

### app.py
- New `make_api_request()` with rate limit handling
- New `process_voicemails_in_batches()` for forward/delete
- Cache management functions: `invalidate_voicemail_cache()`, `get_voicemail_stats_cached()`
- Updated `forward_voicemail_single()` and `delete_voicemail_single()`
- Updated API endpoints to return batch progress info

### templates/forward.html
- Added progress modal during batch operations
- Shows batch count for large selections
- Animated progress bar during processing
- Improved error display (limited to first 10 errors)

### templates/delete.html
- Added progress modal during batch operations
- Shows batch count warning in confirmation
- Animated progress bar during processing
- Links back to dashboard with `?refresh=1`

## UI Changes

### Forward Page
- Info banner explaining batch processing
- "Will process in X batches" message for large selections
- Progress modal with animated progress bar
- Results modal shows success/partial success states

### Delete Page
- Same batch processing indicators
- Warning about batch processing time
- Progress modal during deletion
- Results redirect forces cache refresh

## API Response Changes

### POST /api/forward
```json
{
  "success": true/false,
  "forwarded": 25,
  "failed": 0,
  "total": 25,
  "errors": []
}
```

### POST /api/delete
```json
{
  "success": true/false,
  "deleted": 25,
  "failed": 0,
  "total": 25,
  "errors": []
}
```

## Testing Recommendations

1. **Small batch test:** Forward/delete 5-10 voicemails to verify basic functionality
2. **Medium batch test:** Forward/delete 50-100 voicemails to test batch boundaries
3. **Large batch test:** Forward/delete 200+ voicemails to verify rate limit handling
4. **Cache test:** Delete a voicemail, verify count updates on dashboard refresh

## Deployment

Replace these files in your Render deployment:
1. `app.py` - Main application
2. `templates/forward.html` - Forward page
3. `templates/delete.html` - Delete page

No changes needed to:
- `requirements.txt`
- `render.yaml`
- Other templates (dashboard.html, index.html, etc.)
