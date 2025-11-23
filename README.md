# Genesys Voicemail Manager

A self-service web application for managing Genesys Cloud voicemails. Users can securely log in with their Genesys credentials to view, download, forward, and delete their voicemail messages.

## Overview

Genesys Cloud enforces user-level ownership on voicemail media—administrators cannot access other users' voicemail recordings. This application provides a self-service portal where users can manage their own voicemails through a web browser without installing any software.

## Features

### Voicemail Management
- **View voicemails** - List all voicemails with caller info, date, duration, and read status
- **Download individual** - Download single voicemail as WAV file
- **Download all** - Export all voicemails as ZIP archive with client-side processing
- **Batch downloads** - Smart batching with progress tracking and delays to prevent API throttling
- **Delete individual** - Remove single voicemail with confirmation
- **Bulk delete** - Delete selected or all voicemails with progress tracking
- **Forward to user/group** - Forward voicemail to another Genesys user or group

### User Interface
- **Responsive design** - Works on desktop and mobile devices
- **Color-coded actions** - Green (download), amber (forward), red (delete)
- **Real-time feedback** - Toast notifications for all actions
- **Bulk selection** - Checkbox selection for batch operations

## Security

- **No password storage** - Authentication handled entirely by Genesys Cloud OAuth 2.0 with PKCE
- **Token security** - Access tokens stored only in server-side sessions
- **User isolation** - Each user can only access their own voicemails
- **No server storage** - ZIP files created client-side in browser; voicemails never stored on server
- **HTTPS required** - All production traffic encrypted
- **CSRF protection** - OAuth state parameter prevents cross-site request forgery

## Requirements

- Python 3.8+
- Flask
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
| `/download` | GET | Download page |
| `/forward` | GET | Forward page |
| `/delete` | GET | Delete page |
| `/documentation` | GET | User documentation |
| `/logout` | GET | Clear session and logout |

### OAuth

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/login` | POST | Initiate OAuth flow |
| `/callback` | GET | OAuth callback handler |

### Voicemail Operations

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/voicemails` | GET | List all voicemails (JSON) |
| `/download/<id>` | GET | Download single voicemail (proxy) |
| `/api/delete/<id>` | DELETE | Delete single voicemail |
| `/api/delete-selected` | POST | Delete selected voicemails |
| `/api/forward/<id>` | POST | Forward voicemail to user/group |
| `/api/forward-selected` | POST | Forward selected voicemails |

### Search

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/search/users` | GET | Search users by name/email |
| `/api/search/groups` | GET | Search groups by name |

### Utility

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check endpoint |

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

---

Internal Use Only | Developed by Filip Balakovski
