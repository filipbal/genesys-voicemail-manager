# Voicemail Manager - Performance & Rate Limiting Fixes (v3)

## Issue Analysis

### v1-v2: Rate Limiting Assumption (WRONG)
We initially assumed the errors were caused by Genesys API rate limits. This was incorrect.

### v3: Actual Issue - Gunicorn Worker Timeout
The real problem from the logs:
```
[CRITICAL] WORKER TIMEOUT (pid:57)
```

Gunicorn's default worker timeout is **30 seconds**. Our batch processing with delays exceeded this, causing the worker to be killed mid-operation.

## Solution (v3)

### 1. Gunicorn Configuration (`gunicorn.conf.py`)
```python
timeout = 600  # 10 minutes (was 30 seconds)
graceful_timeout = 120
workers = 2
```

### 2. Updated `render.yaml`
```yaml
startCommand: gunicorn --config gunicorn.conf.py app:app
```

### 3. Rebalanced Batch Settings
Now that we have proper timeout, we can use faster settings:

| Setting | v2 (Too Slow) | v3 (Balanced) |
|---------|---------------|---------------|
| BATCH_SIZE | 15 | **20** |
| BATCH_DELAY | 10s | **3s** |
| OPERATION_DELAY | 0.5s | **0.2s** |
| SUPER_BATCH_SIZE | 3 | **5** |
| SUPER_BATCH_DELAY | 30s | **10s** |

## Time Estimates (v3)

- **50 voicemails:** ~15-20 seconds
- **100 voicemails:** ~30-40 seconds  
- **500 voicemails:** ~3-4 minutes

## Files Changed

1. **NEW: `gunicorn.conf.py`** - Gunicorn configuration with 10-minute timeout
2. **UPDATED: `render.yaml`** - Uses gunicorn config file
3. **UPDATED: `app.py`** - Rebalanced batch settings
4. **UPDATED: `templates/forward.html`** - Updated progress estimates
5. **UPDATED: `templates/delete.html`** - Updated progress estimates

## Deployment Instructions

1. Add `gunicorn.conf.py` to your project root
2. Update `render.yaml` with new startCommand
3. Replace `app.py` and templates
4. Redeploy on Render

The service will automatically restart with the new 10-minute timeout, allowing batch operations to complete successfully.

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
