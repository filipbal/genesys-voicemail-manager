# Genesys Voicemail Manager

> **⛔ END OF LIFE — March 31, 2026**
>
> This application has been decommissioned. The web app has been taken down from Render and the Genesys OAuth client credentials have been revoked.
>
> **Reason:** The business case that prompted this tool has been fulfilled and the associated quality event has been closed.
>
> **Voicemail data is not lost.** All voicemail recordings remain encrypted on Genesys Cloud servers. Supervisors have exported the metadata list. To retrieve a specific recording in the future, report the message ID to the support team — it can be retrieved with a single request via Genesys API Explorer:
>
> ```
> GET /api/v2/voicemail/messages/{messageId}/media
> ```

---

## Overview

Genesys Cloud enforces user-level ownership on voicemail media—administrators cannot access other users' voicemail recordings. This application provided a self-service portal where supervisors could manage their own voicemails through a web browser without installing any software.

## Features

- **View voicemails** — List all voicemails with caller info, date, duration, and read status
- **In-browser playback** — Listen to voicemails via embedded player using temporary media URIs from Genesys API
- **Forward to user/group** — Forward voicemails to another Genesys user or group with 3-phase processing (prepare, populate datatable, forward)
- **Delete individual/bulk** — Remove single or batch delete multiple voicemails with confirmation
- **Export metadata to CSV** — Client-side export of the voicemail table (conversation ID, message ID, caller, dates, duration) for offline record-keeping
- **Group mailbox switching** — Access group voicemail inboxes the user belongs to
- **Original date tracking** — Preserved original timestamps for forwarded voicemails via Genesys Datatables
- **Deleted user cache** — Maintained forwarder display names after user deletion from Genesys via proactive caching

## Architecture

### Security Design

- **No password storage** — Authentication handled entirely by Genesys Cloud OAuth 2.0 with PKCE
- **No file storage** — Audio streamed directly from Genesys Cloud via temporary pre-signed URIs; voicemails never stored on server
- **Token security** — Access tokens stored only in server-side sessions and cleared on logout
- **User isolation** — Each user could only access their own voicemails (permissions inherited from their Genesys token)
- **HTTPS enforced** — All traffic encrypted
- **CSRF protection** — OAuth state parameter prevented cross-site request forgery

### Tech Stack

- **Backend:** Python 3 / Flask / Gunicorn
- **Auth:** Genesys Cloud OAuth 2.0 (Authorization Code with PKCE)
- **Hosting:** Render (web service)
- **Data:** Genesys Cloud Datatables for metadata caching

### Key Design Decisions

- **Rate limit management** — Proactive delays between API calls (0.25s reads, 0.5s writes) to stay within Genesys Cloud's 300 req/min limit, with retry logic for 429 responses
- **Bulk operations** — Batched processing with progress tracking for delete and forward operations
- **Lazy loading** — Dashboard loaded preview first, then full dataset on demand to handle 4,000+ voicemail records
- **Deleted user handling** — When Genesys removes user data on account deletion, the app cached forwarder names proactively to maintain audit trail integrity

## API Reference

### Application Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Home/login page |
| `/dashboard` | GET | Voicemail dashboard |
| `/forward` | GET | Forward page with selection UI |
| `/delete` | GET | Delete page with selection UI |
| `/documentation` | GET | Documentation page |
| `/login` | POST | Initiate OAuth flow |
| `/callback` | GET | OAuth callback handler |
| `/logout` | GET | Clear session and logout |
| `/health` | GET | Health check |
| `/api/forward` | POST | Forward voicemails (batch) |
| `/api/delete` | POST | Delete voicemails (batch) |
| `/api/search/users` | GET | Search users by name/email |
| `/api/search/groups` | GET | Search groups by name |
| `/api/switch-mailbox` | POST | Switch between user/group mailbox |
| `/api/voicemail/<id>/media-url` | GET | Get temporary media URI for playback |
| `/api/load-original-dates` | POST | Load original dates from datatable |

### Genesys Cloud API Calls (Internal)

| Genesys API Endpoint | Method | Description |
|---------------------|--------|-------------|
| `/api/v2/voicemail/search` | POST | Search voicemails (user or group mailbox) |
| `/api/v2/voicemail/messages` | POST | Forward/copy a voicemail |
| `/api/v2/voicemail/messages/{messageId}` | DELETE | Delete a single voicemail |
| `/api/v2/voicemail/messages/{messageId}/media` | GET | Get voicemail media URI |
| `/api/v2/users/me` | GET | Get current user info (with groups expansion) |
| `/api/v2/users/search` | POST | Search users by name/email |
| `/api/v2/groups/{groupId}` | GET | Get group details |
| `/api/v2/groups/search` | POST | Search groups by name |
| `/api/v2/flows/datatables/{id}/rows` | GET/POST | Read/write cached metadata entries |

---

Internal Use Only | Developed by Filip Balakovski
