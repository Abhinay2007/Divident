# Dividend

Dividend turns Debian package inventories into explainable open-source funding proposals. PayPal is restricted to Sandbox; the application does not support production PayPal payments.

## Documentation

| Document | What it covers |
|---|---|
| [Technological Implementation](docs/Technological%20Implementation.md) | Architecture, PayPal integration, AI, allocation engine, and technical decisions. |
| [Design](docs/Design.md) | Product experience, user journey, interface, and usability. |
| [Potential Impact](docs/Potential%20Impact.md) | Problem, beneficiaries, expected outcomes, and measurable impact. |
| [Innovation and Idea](docs/Innovation-Idea.md) | Core concept, differentiation, and novel approach. |
| [Presentation and Demo Guide](docs/Presentation.md) | End-to-end demo sequence and under-three-minute video guide. |

## Recommended: Native Launcher (First)

The recommended first run is `run_dividend.bash`, which runs the API and Vite development server natively on Debian or Ubuntu. **Docker is not required.**

Required system dependencies are Debian/Ubuntu Linux, Git, `dpkg-query`, Python 3.11+, pip, Node.js 20.19+ or 22.12+, and npm. The launcher checks the platform, required commands and versions, project files, `.env`, and Git's ignore rule for `.env`. It creates `.venv` if needed, installs backend and frontend dependencies, scans installed host packages into `.local/scans/`, validates that scan report, then starts Uvicorn and Vite on `127.0.0.1:8000` and `127.0.0.1:5173`. It does not install system prerequisites or create/overwrite credentials.

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip dpkg git
# Install Node.js 20.19+ or 22.12+ and npm from your approved package source.
git clone https://github.com/Abhinay2007/Divident.git
cd Divident
cp .env.example .env
# Edit .env with a local ADMIN_TOKEN and PayPal Sandbox credentials if using PayPal.
chmod +x run_dividend.bash
./run_dividend.bash
```

The launcher reads `.env`; set `ADMIN_TOKEN`, `PUBLIC_BASE_URL` (the `.env.example` default is `http://127.0.0.1:8000`), and, for PayPal mode, `PAYMENTS_MODE=paypal`, `PAYPAL_ENV=sandbox`, `PAYPAL_CLIENT_ID`, and `PAYPAL_CLIENT_SECRET`. Keep secrets out of Git. Open [http://127.0.0.1:5173](http://127.0.0.1:5173); the API is at [http://127.0.0.1:8000](http://127.0.0.1:8000). Import the host scan printed by the launcher via the upload workflow. Press **Ctrl+C** to stop both services. It never approves or captures an order or executes a payout automatically.

Common launcher errors:

| Error | Resolution |
|---|---|
| Unsupported OS or missing prerequisite | Use Debian/Ubuntu and install the required commands; the launcher prints apt suggestions for missing tools. |
| Python or Node.js version too old | Install Python 3.11+ and Node.js 20.19+ or 22.12+ with npm. |
| `.env` missing or not ignored | Copy `.env.example` to `.env`; restore its `.gitignore` protection before starting. The launcher will not create credentials. |
| Dependency install or package scan fails | Check network/package access and the reported error; the launcher checks that the scan output is non-empty and consistent. |
| Port 8000 or 5173 is occupied | Stop the service using the port and rerun the launcher. |

## Alternative: Docker Compose

Compose builds the React/Vite frontend and Python/FastAPI backend into one image. FastAPI serves the browser app and `/api/*` on port 8000. SQLite uses named volume `dividend-local-test-data`, mounted at `/data` in the container.

### Docker prerequisites

Use Debian or Ubuntu with Docker Engine and the Docker Compose v2 plugin. Internet access is needed for image and dependency downloads. PayPal Developer Sandbox credentials are needed for Compose's PayPal mode; Modal credentials are optional for live AI Evidence Review.

### Install or verify Docker

For an existing installation, check `docker --version`, `docker compose version`, and `sudo systemctl status docker`. For a fresh install, use Docker's official apt repository steps for [Debian](https://docs.docker.com/engine/install/debian/) or [Ubuntu](https://docs.docker.com/engine/install/ubuntu/). These are the current commands from those guides; inspect Docker's conflict warnings before replacing an existing engine.

Debian:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: $(. /etc/os-release && echo "$VERSION_CODENAME")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
```

Ubuntu:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
```

Verify the daemon and Compose plugin:

```bash
sudo docker run --rm hello-world
docker compose version
```

If Docker commands fail for socket permissions, use `sudo`. The Docker group grants root-equivalent access; do not add users to it casually.

## Clone and configure

```bash
git clone https://github.com/Abhinay2007/Divident.git
cd Divident
cp .env.example .env
```

Edit `.env` on your machine; never commit it or share its contents. `compose.yaml` requires these values:

| Variable | Purpose |
|---|---|
| `ADMIN_TOKEN` | Non-empty secret protecting administrative operations; replace the template value. |
| `PUBLIC_BASE_URL` | Base URL used for redirects. For local Compose, `http://localhost:8000` is suitable. |
| `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | Credentials from a PayPal Sandbox REST app. |

Compose sets `PAYMENTS_MODE=paypal` and `PAYPAL_ENV=sandbox`. Do not use Live credentials. Create the REST app in the PayPal Developer Dashboard's Sandbox environment. Use a Sandbox business account as seller and a separate Sandbox personal account as buyer; PayPal documents these roles in its [Sandbox account guide](https://developer.paypal.com/sandbox-testing/accounts/). Sandbox checkout uses mock transactions, not real money ([Sandbox overview](https://developer.paypal.com/sandbox-testing/overview/)).

Add the credential keys to `.env` using the Sandbox app values from your own Developer Dashboard:

```dotenv
PAYPAL_CLIENT_ID=your_sandbox_rest_app_client_id
PAYPAL_CLIENT_SECRET=your_sandbox_rest_app_secret
```

Optional configuration from `.env.example`:

| Variables | Purpose |
|---|---|
| `MODAL_ENDPOINT_URL`, `MODAL_MODEL`, `MODAL_PROXY_TOKEN_ID`, `MODAL_PROXY_TOKEN_SECRET` | Modal OpenAI-compatible endpoint and server-side credentials for AI Evidence Review. Tokens must be supplied as a pair. |
| `LLM_LIVE_ENABLED`, `LLM_KILL_SWITCH` | Base Compose keeps live AI off (`false` / `true`). |
| `GITHUB_OAUTH_CLIENT_ID`, `GITHUB_OAUTH_CLIENT_SECRET` | Optional GitHub maintainer claims; configure both or neither. Callback: `<PUBLIC_BASE_URL>/api/maintainer-claims/oauth/callback`. |
| `ENRICH_MODE`, `GITHUB_TOKEN`, `UDD_DSN` | Fixture enrichment is default. Live enrichment requires both GitHub token and UDD DSN. |
| `DATABASE_URL` | Native default is `sqlite:///./data/dividend.db`; Compose overrides to `sqlite:////data/dividend.db`. |
| `DEMO_SEED_ON_BOOT` | If enabled, demo fixtures seed only when the database is empty. |
| `DEMO_SANDBOX_RECIPIENTS_FILE` | Optional normal-demo recipient YAML path inside the backend/container. |
| `SYNTHETIC_SANDBOX_RECIPIENTS_FILE` | Separate recipient file for the fictional Synthetic Sandbox Demo. |

The optional `compose.sandbox-recipients.yaml` overlay mounts a host recipient file read-only at `/run/demo-sandbox-recipients.yaml` and enables live AI review. To use it, set `DEMO_SANDBOX_RECIPIENTS_HOST_PATH` to a valid host file and set both Modal tokens in `.env`.

Normal demo recipients use `DEMO_SANDBOX_RECIPIENTS_FILE`; synthetic demo recipients use `SYNTHETIC_SANDBOX_RECIPIENTS_FILE`. They are separate settings. A path in `.env` must refer to a file mounted or otherwise present inside the container. The base Compose file does not mount the synthetic recipient file; mount it separately if you want to create a synthetic payment demo in Compose.

### Build and start with Docker Compose

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f dividend
```

Open [http://localhost:8000](http://localhost:8000). The same origin serves the React app, static assets, and API. Check container status with `docker compose ps` and health with:

```bash
curl --fail http://localhost:8000/health
```

Health checks database connectivity and returns a status response without credentials.

## Use Dividend and the CLI

The home page supports bundled demo packages, scan-report upload, and pasted Debian source package names. To create a sample upload from the bundled package inventory:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m dividend scan --demo --output ./demo-scan.json
```

Upload `demo-scan.json` in the browser. On a Debian/Ubuntu host with the native environment installed, scan the actual installed package inventory with:

```bash
.venv/bin/python -m dividend scan --output ./host-scan.json
```

That command uses `dpkg-query`; it is intended to run on the host, not in the minimal app container. Both scan commands emit schema-1 JSON containing `origin`, package counts, and source-to-binary membership; output goes to stdout by default and `--output`/`-o` writes to a file. The CLI also supports configuring normal-demo recipients for an existing file-backed SQLite database:

```bash
.venv/bin/python -m dividend configure-sandbox-recipients \
  --database-url sqlite:///./data/dividend.db \
  --file /absolute/path/to/sandbox-recipients.yaml
```

Recipient schema validation is implemented in `dividend/demo.py`. The database must already exist. See actual argument syntax with:

```bash
.venv/bin/python -m dividend --help
.venv/bin/python -m dividend scan --help
.venv/bin/python -m dividend configure-sandbox-recipients --help
```

### Optional AI Evidence Review

The base Compose setup disables live AI. To enable it, provide the mounted recipient file and both Modal tokens, then run:

```bash
docker compose -f compose.yaml -f compose.sandbox-recipients.yaml up -d --build
```

This overlay sets `LLM_LIVE_ENABLED=true` and `LLM_KILL_SWITCH=false`. Evidence Review appears only when a proposal has usable evidence. The model summarizes evidence; it does not calculate allocations. Return to base Compose or set the live flag off and kill switch on to disable calls.

### Stop, restart, and persistence

Stop without deleting the database:

```bash
docker compose down
```

Restart later:

```bash
docker compose up -d
```

Compose stores the DB in named volume `dividend-local-test-data`, mounted at `/data` as `/data/dividend.db`. It survives rebuilds and `docker compose down`. Do not use `docker compose down -v` unless you intend to delete the database volume. Native mode uses `DATABASE_URL` from `.env`, which defaults to `./data/dividend.db`.

## Troubleshooting

| Problem | Check |
|---|---|
| Docker is missing | Follow the official Debian/Ubuntu links above; verify with `docker --version`. |
| Docker socket permission denied | Use `sudo`; add Docker group access only if you accept its root-equivalent privilege. |
| Compose unavailable | Install `docker-compose-plugin` from Docker's repository; verify with `docker compose version`. |
| Port 8000 conflict | Stop the conflicting service or change the host side of `ports` in `compose.yaml` (for example `127.0.0.1:8001:8000`). |
| Container exits | Check `docker compose logs --tail=150 dividend`; verify `.env`, mount permissions, and required variables. Do not share credential-bearing logs. |
| PayPal configuration error | Confirm Sandbox REST app credentials and `PAYPAL_ENV=sandbox`; the application rejects production mode. |
| Payer approval fails | Use a Sandbox personal buyer account in the checkout browser and the app's Sandbox credentials. |
| AI review unavailable | Base Compose disables AI. Check both Modal credentials and explicitly enable it with the overlay; allocation remains deterministic if AI is unavailable. |
| Database/volume error | Check `docker compose ps` and `docker volume inspect dividend-local-test-data`. Do not delete the volume as a troubleshooting shortcut. |

## Security and Sandbox limits

- `.env` is ignored by Git. Keep PayPal, Modal, and OAuth secrets server-side; do not use frontend `VITE_` variables for secrets.
- Use PayPal Sandbox test accounts only. Mock Sandbox transactions do not move real money ([PayPal Sandbox guide](https://developer.paypal.com/sandbox-testing/overview/)).
- Order creation, payer approval, capture, and payout are separate operations. Capture and payout require explicit UI actions.
- The Synthetic Sandbox Demo is visibly labelled and uses fictional contributors/evidence plus its own recipient configuration. It demonstrates orchestration, not real maintainer eligibility.
- SQLite is used by the local Compose workflow. Avoid multiple writers/instances against the same SQLite file.
- The current `render.yaml` selects `PAYMENTS_MODE=fake`; it does not exercise PayPal Sandbox unless that manifest is deliberately changed.
