# Deployment examples

The Python gateway must remain bound to `127.0.0.1`. Put a trusted HTTPS
reverse proxy or tunnel in front of it and forward the original `Host` header.

- `Caddyfile` shows the smallest Caddy configuration.
- `hermes-bridge.service.example` is a generic systemd user service. Adjust
  paths locally; do not commit your filled environment file.

The public hostname must appear in `HERMES_BRIDGE_ALLOWED_HOSTS`, while
`HERMES_BRIDGE_PUBLIC_BASE_URL` contains the same HTTPS origin without `/mcp`.
