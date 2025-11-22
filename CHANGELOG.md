# Voicemail Manager - Performance & Rate Limiting Fixes

## Issues Fixed

### 1. Rate Limiting on Bulk Operations (HTTP 429 Error)
**Problem:** Forwarding 513 voicemails one-by-one hit API rate limits after ~50 operations.

**Solution:** Implemented batch processing with configurable settings:
- `BATCH_SIZE = 25` - Operations per batch
- `BATCH_DELAY = 2.0` - Seconds between batches
- `OPERATION_DELAY = 0.3` - Seconds between operations within a batch
- `MAX_RETRIES = 3` - Automatic retry on 429 with exponential backoff

### 2. Stale Count After Deletion
**Problem:** After deleting a voicemail, the total count (513) wasn't updating.

**Solution:** 
- Added `invalidate_voicemail_cache()` function called after all modifications
- Dashboard now shows refresh parameter `?refresh=1` in back links
- Cache TTL set to 60 seconds for auto-refresh

### 3. Improved API Request Handling
**New:** `make_api_request()` helper function with:
- Automatic retry on 429 (rate limit) responses
- Configurable retry count and backoff
- Proper error handling for all HTTP errors
- Respect for `Retry-After` header when present

## Configuration Parameters

```python
# Rate Limiting and Batch Configuration
API_PAGE_SIZE = 100        # Genesys API max page size
DISPLAY_PAGE_SIZE = 50     # Items per page in UI

BATCH_SIZE = 25            # Operations per batch
BATCH_DELAY = 2.0          # Seconds between batches
OPERATION_DELAY = 0.3      # Seconds between operations
RATE_LIMIT_BACKOFF = 5.0   # Default backoff on 429
MAX_RETRIES = 3            # Max retries per request

CACHE_TTL = 60             # Cache validity in seconds
```

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
