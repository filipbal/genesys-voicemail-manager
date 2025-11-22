# Voicemail Manager - Performance & Rate Limiting Fixes (v2)

## Issues Fixed

### 1. Rate Limiting on Bulk Operations (HTTP 429 Error)
**Problem:** Forwarding/deleting voicemails hit API rate limits after ~50 operations, even with batching.

**Root Cause:** Genesys has a rolling ~50 request limit that resets over time, not just a per-batch limit.

**Solution:** Implemented aggressive two-tier batching with longer delays:
- **Smaller batches:** `BATCH_SIZE = 15` (well under the 50 limit)
- **Longer delays:** `BATCH_DELAY = 10.0s` between batches
- **Super batches:** After every 3 batches, take a 30-second break
- **Emergency backoff:** If 3+ consecutive failures detected, wait 60 seconds
- **Automatic retry:** Up to 5 retries on 429 with 30s backoff

### 2. Stale Count After Deletion
**Problem:** After deleting a voicemail, the total count wasn't updating.

**Solution:** 
- Added `invalidate_voicemail_cache()` function called after all modifications
- Dashboard now shows refresh parameter `?refresh=1` in back links
- Cache TTL set to 60 seconds for auto-refresh

### 3. Improved Error Detection
**New:** The batch processor now detects rate limiting patterns:
- Tracks consecutive failures
- If 3+ failures in a row, triggers emergency delay
- Reports `rate_limited: true` in results if rate limiting was detected

## Configuration Parameters (v2)

```python
# Rate Limiting and Batch Configuration - CONSERVATIVE
API_PAGE_SIZE = 100        # Genesys API max page size
DISPLAY_PAGE_SIZE = 50     # Items per page in UI

BATCH_SIZE = 15            # Operations per batch (keep well under 50)
BATCH_DELAY = 10.0         # Seconds between batches
OPERATION_DELAY = 0.5      # Seconds between operations
RATE_LIMIT_BACKOFF = 30.0  # Default backoff on 429
MAX_RETRIES = 5            # Max retries per request

# Super batch - prevents hitting rolling rate limits
SUPER_BATCH_SIZE = 3       # Number of batches before long break
SUPER_BATCH_DELAY = 30.0   # Seconds for super batch break

CACHE_TTL = 60             # Cache validity in seconds
```

## Time Estimates

With these settings, operations will take approximately:
- **50 voicemails:** ~1 minute
- **100 voicemails:** ~2-3 minutes
- **500 voicemails:** ~15-20 minutes

The UI now shows more realistic progress estimates.

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
