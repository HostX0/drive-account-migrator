# Security

Never include OAuth client JSON, access/refresh tokens, controller contents, source/destination IDs, private filenames, SQLite journals or screenshots containing account information in a public issue.

For a suspected vulnerability, use GitHub's private vulnerability reporting for this repository when available. Otherwise open a minimal issue asking for a private reporting channel without including secrets or exploit details affecting an account.

The UI is a local utility, not a production web service. It binds only to loopback, requires a random session token for every API operation, checks Host/Origin, and uses a restrictive content security policy. Do not expose it via tunnels, port forwarding or a public host. Local users/processes with access to your profile may still read your credentials; use normal OS account protection and disk encryption.

The app uses Google OAuth rather than passwords or API keys. Review every consent screen and revoke access when finished. The full Drive scope is broad even though worker mutations are restricted. Public source code and test fixtures must never contain real credentials or personal inventories.
