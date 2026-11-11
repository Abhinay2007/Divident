# Dividend

Dividend turns a Debian package inventory into an explainable funding plan. Allocation explanations are deterministic. The optional AI Evidence Review runs only when a user selects it.

## Modal AI Evidence Review

The backend calls the configured Modal Dedicated Endpoint. Add these server-side settings to `.env` (copy `.env.example`); never add proxy tokens to frontend configuration:

```dotenv
MODAL_ENDPOINT_URL=https://asthorat7-pune--ep-deepseek-v4-1-flash-1017-server.us-west.modal.direct/v1
MODAL_MODEL=deepseek-ai/DeepSeek-V4.1-Flash-1017
MODAL_PROXY_TOKEN_ID=your-token-id
MODAL_PROXY_TOKEN_SECRET=your-token-secret
LLM_LIVE_ENABLED=true
LLM_KILL_SWITCH=false
```

For local development, keep `LLM_LIVE_ENABLED=false` and `LLM_KILL_SWITCH=true` to disable endpoint calls. The evidence review sends only validated contribution evidence, applies a 2,500-token input budget, and validates all contributor IDs and PR citations before displaying results.
