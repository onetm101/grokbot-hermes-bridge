# Security Policy

## Supported version

Security fixes target the latest release on the default branch.

## Safe operation

- Keep the gateway bound to `127.0.0.1` and terminate TLS at a trusted reverse
  proxy or tunnel.
- Use a unique random `HERMES_BRIDGE_SECRET` of at least 32 characters. Rotate
  it if it was ever pasted into chat, committed, or logged.
- Never add an `Authorization` header to the plugin MCP config. Let the client
  complete OAuth discovery and PKCE.
- Protect the environment file and OAuth client store with mode 0600.
- Keep `HERMES_BRIDGE_ALLOWED_HOSTS` narrow and update dependencies regularly.
- Treat all Hermes output as potentially sensitive. The gateway bounds it but
  cannot decide which facts your agent is allowed to disclose.

## Known limitations

The bundled OAuth provider is for a single trusted owner. Refresh tokens are
signed and expire, but there is no per-token revocation database. Rotate the
owner secret to invalidate every issued token. Use a managed identity provider
if you need multiple users, central revocation, or organization policy.

## Reporting a vulnerability

Open a GitHub security advisory on this repository. Do not include live tokens,
environment dumps, private hostnames, or conversation content in a public
issue.
