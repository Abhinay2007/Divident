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

## Run Dividend Natively on Debian/Ubuntu (No Docker)

Prerequisites: Debian or Ubuntu, Python 3.11+, pip, Node.js 20.19+ (or 22.12+) with npm, and `dpkg-query`. On a minimal system, install Python and dpkg tools with `sudo apt install python3 python3-venv python3-pip dpkg`; install a supported Node.js release and npm from your approved package source.

```bash
git clone <repository-url>
cd dividend
cp .env.example .env
# Edit .env with your local backend configuration; keep PayPal in sandbox mode.
chmod +x run_dividend.bash
./run_dividend.bash
```

The launcher creates/reuses `.venv`, installs the project from `pyproject.toml` and frontend dependencies from the lockfile, and scans installed packages on the host. Scan reports are saved under `.local/scans/` and are ignored by Git. Upload the printed report through Dividend's existing package upload flow; the launcher does not import it or approve/capture payments. Open the frontend at http://127.0.0.1:5173; the backend is at http://127.0.0.1:8000. Press Ctrl+C to stop both services.

Troubleshooting: missing system tools are reported with Debian/Ubuntu install commands. The launcher requires Python 3.11+ and a Vite-compatible Node.js version. If port 8000 or 5173 is occupied, stop the service using it and rerun; the launcher does not kill unrelated processes. If startup configuration fails, review `.env` locally without sharing its secrets.

## Deploy on Render

`render.yaml` defines one Docker Web Service on the paid Starter plan, with one instance and a persistent disk mounted at `/var/data`. SQLite is stored at `/var/data/dividend.db`; keep the service at one instance. The app serves the Vite build and API from the same origin.

Create the service from the repository in Render using the Blueprint flow and `render.yaml`. Supply `PUBLIC_BASE_URL` and the PayPal Sandbox REST app values for `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET`. Do not use live PayPal credentials. The manifest generates `ADMIN_TOKEN`. GitHub maintainer claims are optional; configure both `GITHUB_OAUTH_CLIENT_ID` and `GITHUB_OAUTH_CLIENT_SECRET` together if needed.

Modal AI Evidence Review is off by default. To enable it, configure both `MODAL_PROXY_TOKEN_ID` and `MODAL_PROXY_TOKEN_SECRET`, then set `LLM_LIVE_ENABLED=true` and `LLM_KILL_SWITCH=false` in the Render environment. The endpoint and model are preconfigured with `MODAL_ENDPOINT_URL` and `MODAL_MODEL`. The kill switch must remain enabled unless live AI review is intentionally enabled for the demo.

Render checks `/health`. The persistent disk makes deploys briefly unavailable and is accessible to only one service instance; keep regular database backups. Render disk data is unavailable during image builds and pre-deploy commands.
