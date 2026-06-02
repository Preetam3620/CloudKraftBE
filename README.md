# CloudKraft — Backend

Writing Terraform by hand is tedious. Getting VPC → Subnet → Security Group → EC2 references right takes experience most developers don't have, and the feedback loop (write → validate → fix → repeat) is slow.

CloudKraft is a visual AWS infrastructure designer. You drag resources onto a canvas, connect them, and get valid Terraform HCL out. From there you can validate it, estimate cost, and deploy — all in the browser, against real AWS.

There's also an AI assistant (Claude / OpenAI / Gemini) that lets you describe infrastructure in plain English and watch it appear on the canvas.

This is the FastAPI backend. The frontend lives in `CloudKraftFE/`.

## How it works

1. **Canvas → Code**: The frontend sends a workflow JSON (nodes + connections). The backend maps resource types and cross-resource references to Terraform attributes via `REFERENCE_ATTRIBUTE_MAP` and outputs `main.tf`, `variables.tf`, `outputs.tf`.

2. **Validation**: Runs real `terraform validate -json` against a prewarmed workspace (provider pre-downloaded on startup, ~2s per validation). Falls back to static regex checks with CIS-inspired security rules if the binary isn't available.

3. **Deployment**: `POST /api/deploy/apply` returns 202 immediately; `terraform apply` runs in a thread. Full lifecycle: `pending → planning → planned → running → succeeded/failed`. Each deployment gets an isolated workspace. Frontend polls logs every 2 seconds.

4. **AI chat**: LangGraph manages multi-turn conversation state. Supports three providers with different SDKs behind a unified interface. Streams over SSE (REST) or WebSocket — both in the same backend.

## Supported AWS resources

EC2, Lambda, Auto Scaling, VPC, Subnet, Security Group, Internet Gateway, Route Table, NAT Gateway, Elastic IP, Load Balancer, S3, EFS, EBS, RDS, DB Subnet Group, DynamoDB, SNS, SQS, IAM Role, CloudFront

## Setup

```bash
python -m venv venv
venv\Scripts\activate       # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env`. Minimum required:

```
SECRET_KEY=       # python -c "import secrets; print(secrets.token_urlsafe(32))"
ENCRYPTION_KEY=   # same
ANTHROPIC_API_KEY=
```

```bash
uvicorn app.main:app --reload --port 8000
```

DB tables are auto-created on first run. API docs at `http://localhost:8000/docs`.

To pre-generate the AWS provider schema (speeds up codegen, ~50 MB):

```bash
python scripts/generate_aws_schema.py
```

## Environment variables

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./cloudkraft.db` | Supports PostgreSQL |
| `SECRET_KEY` | — | JWT signing |
| `ENCRYPTION_KEY` | — | AWS credential encryption |
| `CORS_ORIGINS` | `http://localhost:5173` | Comma-separated |
| `ANTHROPIC_API_KEY` | — | Required for AI features |
| `OPENAI_API_KEY` | — | Optional, for chat |
| `BACKEND_AWS_ACCESS_KEY` | — | For IAM role assumption |
| `BACKEND_AWS_SECRET_KEY` | — | Paired with above |
| `TF_STATE_BUCKET` | — | S3 bucket for remote state |
| `TF_STATE_LOCK_TABLE` | — | DynamoDB table for state locking |

## Project structure

```
app/
├── api/          # auth, workflows, codegen, deploy, chat, ai
├── models/       # User, Workflow, Deployment, ChatSession, AuditLog
├── schemas/      # Pydantic request/response types
├── services/     # terraform_generator, terraform_deployer, chat_graph, cost_estimator
└── utils/        # encryption, security helpers
```

## Stack

Python · FastAPI · SQLAlchemy · LangGraph · boto3 · Terraform · Argon2 · Fernet
