# Google OAuth setup

The app uses the installed-application OAuth flow, PKCE, a random state value and a loopback callback. Passwords are entered only on Google's pages. An API key or a ChatGPT connector link ID is not an OAuth credential. [Official installed-app authorization documentation](https://developers.google.com/identity/protocols/oauth2/native-app).

## Create your own desktop client

1. Open Google Cloud Console and create a project you control.
2. Enable Google Drive API and Google Docs API in the API Library.
3. Configure Google Auth Platform for External users in Testing mode. Add both accounts as test users.
4. Create a **Desktop app** OAuth client and download its JSON file.
5. Launch the local interface, choose the JSON file and connect each account. Check the account shown on Google's consent screen. Consent and account verification must succeed before a token is saved.

The launcher never enables billing, starts a trial or buys credits. This workflow does not need a paid API key. For large jobs, review the current [Drive API quota and billing thresholds](https://developers.google.com/workspace/drive/api/guides/limits) and [Docs API limits](https://developers.google.com/workspace/docs/api/limits); do not assume unlimited API usage.

## Accounts and permissions

| Connection | Requested OAuth scope | Purpose |
|---|---|---|
| source | `https://www.googleapis.com/auth/drive.readonly` | List and verify originals, including trashed originals. |
| destination | `https://www.googleapis.com/auth/drive` | Create destination-owned copies, controller checkpoints and optional sharing grants. |
| source-cleanup | `https://www.googleapis.com/auth/drive` | A separately authorized connection for reviewed Trash/purge actions. |

The full Drive scope technically allows broader actions than the worker uses. The copy client rejects source writes and destination deletes/updates. The cleanup client accepts only reviewed source IDs and supported cleanup operations. You can revoke app access from your Google account.

The source folder must be readable by the destination account. Share it as Viewer in Google Drive and leave copying allowed. This is distinct from optionally giving the old account access to the *new* copies. Restricted items remain excluded. [Google Drive permission concepts](https://developers.google.com/workspace/drive/api/guides/manage-sharing).

Use two personal folders with identical names, each owned by its corresponding account. Only the configured source subtree is considered. Both identities and ownership are checked live. Shared drives and account-wide Gmail/Photos migration are outside this release.

## Controller and state

For a new migration, create an empty destination folder yourself, then press **Create controller once**. The command creates a native Google Doc owned by the destination account and saves its ID in your private configuration. It never creates another destination root. A lost controller-creation response leaves an intent receipt and stops instead of silently creating a duplicate.

For an existing migration, load its configuration and keep the same state directory and controller ID. The controller is JSON in a text-only document. Reads include all Docs tabs and nested tabs; writes require the JSON to occupy one nonempty text tab. Multi-tab JSON is read but cannot be rewritten automatically. See [safety and recovery](safety.md).

Default configuration: `~/.config/drive-account-migrator/config.json`.
Default private state: `~/.local/state/drive-account-migrator/`.

Use a directory you control outside the checkout. Unix writes use private permissions; on Windows, use your private user profile and protect its ACLs. Tokens, inventories and reports are never meant to be committed.

## Common problems

- **Missing connection:** connect the indicated account before preview/copy. Do not paste a token into the UI.
- **Wrong account:** no token is saved; start the intended consent flow again.
- **Token already exists:** the helper refuses to overwrite it. Preserve the current state, revoke/rotate credentials deliberately if necessary, and reauthorize only the affected role. Tokens from Testing apps may require reauthorization; follow Google's current policy.
- **Access denied or copy restricted:** leave the item excluded. Do not attempt to bypass the restriction.
- **Another live lease:** let its worker finish or its bounded lease expire; never clear another worker's lease.
- **Old destination is not empty:** import trusted mappings or review conflicts; name-only native/folder adoption is refused.
- **Browser callback warning:** do not bypass browser security warnings. Check the local terminal result and legitimate network/browser configuration.
- **Storage remains full:** copying does not free source storage, and Trash still counts. Permanent deletion is separate and irreversible. Storage updates may lag. [Google storage help](https://support.google.com/drive/answer/6374270?hl=en).
- **UI reload:** the local session is kept in browser session storage. A server restart uses a new port/token; reopen the fresh launcher URL.

You need only Python's standard library and a modern browser. The interface binds to `127.0.0.1`, uses a random session token, rejects cross-origin API calls and loads no remote scripts or analytics. Do not expose its port through a tunnel or public reverse proxy.
