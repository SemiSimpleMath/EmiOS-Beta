# Google OAuth Setup for Alpha Testers

## Overview
Emi now uses Google OAuth for Gmail, Calendar, and Tasks access. This is more secure than IMAP and gives you access to all Google services.

## What You Need

### 1. Google Cloud Console Setup (Already Done)
The developer has already set up the OAuth client credentials. You just need to be added as a test user.

### 2. Your Google Account
You'll use your own Google account to authenticate. The app will request permission to:
- Read and send emails
- Access your calendar
- Manage your tasks

## First-Time Setup

### During Installation

When you run `setup.py`, you'll see Step 6 with an optional section:

```
Optional: Connect Google Services
[Authenticate with Google] button
```

**To connect Google services:**
1. Click "Authenticate with Google"
2. A browser window will open
3. Log in with your Google account
4. Review the permissions and click "Allow"
5. The window will close automatically
6. Setup wizard will show "✓ Google services connected successfully!"

**To skip (you can do it later):**
- Just ignore the button and click "Next Step"
- You can connect later from Settings

### After Installation

If you skipped OAuth during setup, you can connect later:

1. Open Emi in your browser
2. Click Settings (⚙️ icon)
3. Go to "Features" tab
4. Find Email, Calendar, or Tasks features
5. Click "Connect Google Account" button
6. Follow the authentication steps
7. Features will become available

## What Happens Behind the Scenes

### Files Created
After authentication, a file is created at:
```
app/assistant/lib/core_tools/credentials/token.pickle
```

This file contains your Google OAuth token. It's specific to your Google account and allows Emi to access your Google services without storing your password.

### Security
- Your Google password is **never** stored by Emi
- The token can only be used by Emi on your computer
- You can revoke access anytime from your Google Account settings
- The token automatically refreshes when needed

## Managing Your Connection

### Checking Status
In Settings > Features, you'll see:
- ✓ Green = Connected and working
- 🔒 Gray = Not connected (with "Connect Google Account" button)

### Disconnecting
To disconnect your Google account:
1. Delete the file: `app/assistant/lib/core_tools/credentials/token.pickle`
2. Or go to your [Google Account](https://myaccount.google.com/permissions) and revoke access to "Emi"

### Reconnecting
If your token expires or you disconnect:
1. Go to Settings > Features
2. Click "Connect Google Account" on any Google feature
3. Re-authenticate

## Troubleshooting

### "Access blocked: This app isn't verified"
**This is normal for alpha testing!**

You'll see this message because the app hasn't been verified by Google yet. This is expected during alpha testing.

**Solution**: The developer needs to add your email as a test user.
1. Contact the developer
2. Provide your Google account email address
3. Developer adds you in Google Cloud Console
4. Try authenticating again

### OAuth popup is blocked
Your browser might block the popup window.

**Solution**:
1. Look for a popup blocker icon in your address bar
2. Click "Always allow popups from this site"
3. Try clicking "Authenticate with Google" again

### "OAuth credentials not found"
This means the OAuth client configuration is missing.

**Solution**: Contact the developer - they need to include `credentials.json` in the alpha package.

### Features still disabled after authenticating
**Solution**:
1. Refresh the Features Settings page
2. Check that `token.pickle` exists in `app/assistant/lib/core_tools/credentials/`
3. Try disconnecting and reconnecting

### Can't access Google services
If Emi says "Failed to authenticate with Google":

**Solution**:
1. Delete `app/assistant/lib/core_tools/credentials/token.pickle`
2. Re-authenticate through Settings > Features
3. If still failing, check your internet connection
4. Make sure your Google account is active

## Privacy & Permissions

### What Emi Can Access
After you authenticate, Emi can:
- **Email**: Read your emails and send emails on your behalf
- **Calendar**: View and create calendar events
- **Tasks**: View and manage your Google Tasks

### What Emi Cannot Access
- Your Google password
- Other Google services (Drive, Photos, etc.)
- Your contacts (unless explicitly added in the future)

### Revoking Access
You can revoke Emi's access anytime:
1. Go to [Google Account Permissions](https://myaccount.google.com/permissions)
2. Find "Emi" in the list
3. Click "Remove Access"

Emi will stop working with Google services until you re-authenticate.

## For Developers Adding Test Users

If you're helping test and need to add more users:

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Select your project
3. Go to "APIs & Services" > "OAuth consent screen"
4. Scroll to "Test users"
5. Click "Add Users"
6. Enter email addresses (one per line)
7. Click "Save"

Test users can now authenticate even though the app isn't verified.

## Questions?

If you run into any issues:
1. Check this guide first
2. Look at the main documentation: `GOOGLE_OAUTH_IMPLEMENTATION.md`
3. Contact the developer with:
   - Error message (if any)
   - Steps you took before the error
   - Screenshot of the issue

## Quick Reference

| Task | Location |
|------|----------|
| Initial setup | Run `setup.py`, Step 6 |
| Connect later | Settings > Features > Connect Google Account |
| Check status | Settings > Features (green ✓ or gray 🔒) |
| Revoke access | Delete `token.pickle` or Google Account settings |
| Token location | `app/assistant/lib/core_tools/credentials/token.pickle` |

---

**Note**: This OAuth setup is required for Email, Calendar, and Tasks features. Other Emi features (Weather, News, Daily Summary, etc.) work independently and don't require Google authentication.

