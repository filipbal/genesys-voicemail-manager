# Genesys Cloud Voicemail Web Exporter

A self-service web application that allows Genesys Cloud users to export their own voicemails via browser. Designed for PythonAnywhere deployment.

## Background

Genesys Cloud API enforces user-level ownership on voicemail media - administrators cannot access other users' voicemail recordings (returns 403 Forbidden). Only the voicemail owner can download their own recordings.

This web portal allows 50+ departing employees to export their voicemails without installing any software.

## Features

- **OAuth PKCE Authentication**: Secure login via Genesys Cloud
- **Multi-Region Support**: Works with all Genesys Cloud regions (EMEA, US, Asia, etc.)
- **Self-Service**: Users log in with their own credentials and access only their own voicemails
- **Individual Downloads**: Download voicemails one at a time as WAV files
- **Bulk Export**: Download all voicemails as a ZIP file with metadata
- **No Database Required**: Stateless, session-based operation
- **Automatic Cleanup**: Temporary files are automatically deleted after 1 hour

## Prerequisites

### 1. Genesys Cloud OAuth Client

Create an OAuth client in Genesys Admin:

1. Go to **Admin > Integrations > OAuth**
2. Click **Add Client**
3. Configure:
   - **App Name**: Voicemail Exporter
   - **Grant Types**: Check **Code Authorization**
   - **Redirect URI**: `https://<username>.pythonanywhere.com/callback`
   - **Scopes**: Select `voicemail` and `voicemail:readonly`
4. Save and copy the **Client ID**

> **Important**: You need to create this OAuth client in EACH Genesys organization/region where users need to export voicemails.

### 2. PythonAnywhere Account

- Free tier works for testing
- Paid tier recommended for production (better performance, HTTPS)

## Deployment on PythonAnywhere

### Step 1: Upload Files

1. Log in to PythonAnywhere
2. Go to **Files** tab
3. Create a new directory: `/home/<username>/voicemail_exporter`
4. Upload all files:
   - `app.py`
   - `requirements.txt`
   - `wsgi.py`
   - `templates/` folder (with all HTML files)

### Step 2: Install Dependencies

1. Go to **Consoles** tab
2. Start a **Bash** console
3. Run:
   ```bash
   cd ~/voicemail_exporter
   pip3 install --user -r requirements.txt
   ```

### Step 3: Configure Web App

1. Go to **Web** tab
2. Click **Add a new web app**
3. Select **Manual configuration**
4. Choose **Python 3.10** (or latest available)
5. Set **Source code** directory: `/home/<username>/voicemail_exporter`
6. Edit **WSGI configuration file**:
   - Replace the content with the content from `wsgi.py`
   - Update the `project_home` path with your username

### Step 4: Set Environment Variables

In the **Web** tab, scroll to **Environment variables** section and add:

| Variable | Value |
|----------|-------|
| `GENESYS_CLIENT_ID` | Your OAuth Client ID from Genesys |
| `REDIRECT_URI` | `https://<username>.pythonanywhere.com/callback` |
| `FLASK_SECRET_KEY` | A random string (generate with `python -c "import secrets; print(secrets.token_hex(32))"`) |

### Step 5: Reload Web App

Click the **Reload** button in the Web tab.

### Step 6: Test

Visit `https://<username>.pythonanywhere.com` and test the login flow.

## Local Development

For local testing:

1. Clone/download the files
2. Install Flask: `pip install flask`
3. Set environment variables or edit `app.py`:
   ```python
   CLIENT_ID = 'your-client-id'
   REDIRECT_URI = 'http://127.0.0.1:5000/callback'
   ```
4. Update Genesys OAuth client to include `http://127.0.0.1:5000/callback` as a redirect URI
5. Run: `python app.py`
6. Visit: `http://127.0.0.1:5000`

## File Structure

```
voicemail_exporter/
├── app.py              # Main Flask application
├── requirements.txt    # Python dependencies
├── wsgi.py            # WSGI config for PythonAnywhere
├── README.md          # This file
└── templates/
    ├── base.html      # Base template with Bootstrap
    ├── index.html     # Login page
    ├── dashboard.html # Voicemail list and download
    └── error.html     # Error pages
```

## Security Considerations

1. **No Password Storage**: User passwords are never seen or stored by this application. Authentication is handled entirely by Genesys Cloud OAuth.

2. **Token Security**: Access tokens are stored only in server-side sessions and expire automatically.

3. **User Isolation**: Each user can only access their own voicemails due to Genesys API's user-level ownership enforcement.

4. **Temporary Files**: Downloaded files are stored temporarily and automatically cleaned up after 1 hour.

5. **HTTPS**: PythonAnywhere provides HTTPS by default.

6. **CSRF Protection**: OAuth state parameter prevents cross-site request forgery.

## Troubleshooting

### "OAuth Client ID not configured"
- Set the `GENESYS_CLIENT_ID` environment variable in PythonAnywhere Web tab

### "Invalid redirect URI"
- Ensure the redirect URI in Genesys OAuth client matches exactly: `https://<username>.pythonanywhere.com/callback`
- Check for trailing slashes

### "Access denied" when downloading
- The user doesn't own that voicemail
- The voicemail may have been deleted
- Try logging out and back in

### No voicemails showing
- The user's voicemail inbox is empty
- Check if the correct region is selected

### Timeout errors
- PythonAnywhere free tier has limited CPU
- Consider upgrading for better performance
- Large voicemail downloads may time out

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Home/login page |
| `/login` | POST | Initiate OAuth flow |
| `/callback` | GET | OAuth callback handler |
| `/dashboard` | GET | View voicemails |
| `/download/<id>` | GET | Download single voicemail |
| `/download-all` | GET | Download all as ZIP |
| `/logout` | GET | Clear session |
| `/health` | GET | Health check |
| `/api/voicemails` | GET | JSON API for voicemails |

## Support

For issues with:
- **This application**: Contact your IT administrator
- **Genesys Cloud**: Contact Genesys support
- **PythonAnywhere**: Check their help pages

## License

Internal use only - Novartis

## Author

Filip Balakovski
