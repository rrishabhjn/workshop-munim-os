# Workshop Munim OS

Unified operations, production tracking, and bookkeeping companion for a custom made-to-order furniture workshop in Beawar, Rajasthan, India.

## Architecture
- **Backend:** Python FastAPI (`backend/`) — Telegram Bot webhook + Cloud Tasks worker, Gemini multimodal router (`gemini-4-hydrogen` / `gemini-3.5-flash-lite`), Zoho Books India (`zohoapis.in`) draft-only client, REST API, and cron endpoints (`08:30 IST` & `02:00 IST`).
- **Frontend:** React + Tailwind PWA & Telegram Mini App (`frontend/`).
- **Database & Auth:** Supabase PostgreSQL + Google Sign-In (`ap-south-1` Mumbai).
- **File Storage:** Private Google Cloud Storage bucket (`asia-south1` Mumbai) with V4 signed URLs.

## Local Development

### 1. Environment
Copy `.env.example` to `.env` and fill in your Wave 1 credentials.

### 2. Backend
```bash
cd backend
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
pytest
uvicorn app.main:app --reload --port 8000
```

### 3. Frontend
```bash
cd frontend
npm install
npm run dev
```

## Cloud Run Deployment (`asia-south1`)
```bash
gcloud builds submit --tag asia-south1-docker.pkg.dev/$GCP_PROJECT_ID/workshop-munim/app:latest .
gcloud run deploy workshop-munim-os \
  --image asia-south1-docker.pkg.dev/$GCP_PROJECT_ID/workshop-munim/app:latest \
  --region asia-south1 \
  --allow-unauthenticated
```
