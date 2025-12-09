# Genesys Voicemail Manager

A self-service web application for managing Genesys Cloud voicemails. Users can securely log in with their Genesys credentials to view, download, forward, and delete their voicemail messages.

## Overview

Genesys Cloud enforces user-level ownership on voicemail media—administrators cannot access other users' voicemail recordings. This application provides a self-service portal where users can manage their own voicemails through a web browser without installing any software.

## Features

### Voicemail Management
- **View voicemails** - List all voicemails with caller info, date, duration, and read status
- **Download individual** - Download single voicemail as WAV file
- **Download bulk** - Export multiple voicemails as ZIP archive with metadata
- **Delete individual** - Remove single voicemail with confirmation
- **Delete bulk** - Batch delete multiple voicemails with confirmation
- **Forward to user/group** - Forward voicemail to another Genesys user or group

## Security

- **No password storage** - Authentication handled entirely by Genesys Cloud OAuth 2.0 with PKCE
- **No file storage** - ZIP files created client-side in browser; voicemails never stored on server
- **Token security** - Access tokens stored only in server-side sessions and cleared on logout
- **User isolation** - Each user can only access their own voicemails
- **HTTPS required** - All traffic encrypted
- **CSRF protection** - OAuth state parameter prevents cross-site request forgery

## Supported Genesys Regions

| Region | Host |
|--------|------|
| US West | usw2.pure.cloud |

## Requirements

- Python 3
- Flask
- Gunicorn (production)
- Genesys Cloud OAuth Client (Code Authorization with PKCE)

### Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `GENESYS_CLIENT_ID` | OAuth Client ID from Genesys | Yes |
| `REDIRECT_URI` | OAuth callback URL | Yes |
| `FLASK_SECRET_KEY` | Secret key for session encryption | Yes |

## API Endpoints

### Pages

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Home/login page |
| `/dashboard` | GET | Voicemail dashboard |
| `/download` | GET | Download page with selection UI |
| `/forward` | GET | Forward page with selection UI |
| `/delete` | GET | Delete page with selection UI |
| `/logout` | GET | Clear session and logout |
| `/documentation` | GET | Documentation page |

### OAuth

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/login` | POST | Initiate OAuth flow |
| `/callback` | GET | OAuth callback handler |

### Voicemail Operations

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/download/<id>` | GET | Download single voicemail as WAV |
| `/api/forward` | POST | Forward voicemails (batch - accepts JSON with voicemail_ids, target_id, target_type) |
| `/api/delete` | POST | Delete voicemails (batch - accepts JSON with voicemail_ids) |

**Note:** The application uses POST for batch operations to send JSON payloads. Internally, it makes the appropriate HTTP method calls (DELETE for deletes, POST for forwards) to the Genesys Cloud API.

### API Endpoints (JSON)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/search/users` | GET | Search users by name/email (uses Genesys POST internally) |
| `/api/search/groups` | GET | Search groups by name (uses Genesys POST internally) |
| `/api/switch-mailbox` | POST | Switch between user/group mailbox (accepts JSON with type, id, name) |
| `/api/voicemail/<id>/media-url` | GET | Get temporary media URL for audio playback |
| `/api/load-original-dates` | POST | Load original dates from datatable (accepts JSON with conversation_ids) |

### Utility

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check endpoint |

### Genesys Cloud API Calls

The application internally makes these calls to Genesys Cloud:

| Genesys API Endpoint | Method | Description |
|---------------------|--------|-------------|
| `/api/v2/voicemail/search` | POST | Search voicemails (user or group mailbox) |
| `/api/v2/voicemail/messages` | POST | Forward/copy a voicemail |
| `/api/v2/voicemail/messages/{messageId}` | DELETE | Delete a single voicemail |
| `/api/v2/voicemail/messages/{messageId}/media` | GET | Get voicemail media URL |
| `/api/v2/users/me` | GET | Get current user info (with groups expansion) |
| `/api/v2/users/search` | POST | Search users by name/email |
| `/api/v2/groups/{groupId}` | GET | Get group details |
| `/api/v2/groups/search` | POST | Search groups by name |
| `/api/v2/flows/datatables/{id}/rows` | GET/POST | Read/write original date entries |

## Troubleshooting

### "OAuth Client ID not configured"
- Ensure `GENESYS_CLIENT_ID` environment variable is set

### "Invalid redirect URI"
- Verify the redirect URI in Genesys OAuth client matches `REDIRECT_URI` exactly
- Check for trailing slashes

### "Access denied" when downloading
- The voicemail may have been deleted
- Try logging out and back in to refresh the token

### No voicemails showing
- The voicemail inbox may be empty
- Verify the correct region is selected

### Users or Groups not found when searching
- Ensure `users:readonly` and `groups:readonly` scope is added to OAuth client

## OAuth Scopes Required

The Genesys OAuth client must have the following scopes:

- `voicemail` - Read and manage voicemails
- `users:readonly` - Search users for forwarding
- `groups:readonly` - Search groups for forwarding

## Production

The application is configured for deployment on Render.

---

Internal Use Only | Developed by Filip Balakovski