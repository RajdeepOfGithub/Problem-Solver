# 🔐 Vega — Environment Configuration

> See also: [[ARCHITECTURE]] | [[API]] | [[Roadmap]]

---

> [!WARNING] Security — Read This First
> **Never commit `.env` to GitHub.** Add it to `.gitignore` immediately before creating it.
> The `.env` file contains AWS credentials, GitHub tokens, and API keys that provide full access to your cloud infrastructure. A leaked credential is a critical incident — not a minor mistake.
> Run `echo ".env" >> .gitignore` before touching any of the variables below.

---

## 1. Full `.env` Template

Copy this file to `.env` at the project root and fill in all required values before running anything.

```bash
# ─────────────────────────────────────────────
# AWS Core
# ─────────────────────────────────────────────
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_REGION=us-east-1
AWS_BEDROCK_REGION=us-east-1

# ─────────────────────────────────────────────
# Amazon Nova — Model IDs
# ─────────────────────────────────────────────
NOVA_LITE_MODEL_ID=amazon.nova-2-lite-v1:0
NOVA_SONIC_MODEL_ID=amazon.nova-2-sonic-v1:0
NOVA_EMBEDDING_MODEL_ID=amazon.nova-2-multimodal-embeddings-v1:0

# ─────────────────────────────────────────────
# Nova Act
# ─────────────────────────────────────────────
NOVA_ACT_API_KEY=

# ─────────────────────────────────────────────
# GitHub
# ─────────────────────────────────────────────
GITHUB_TOKEN=
GITHUB_APP_CLIENT_ID=
GITHUB_APP_CLIENT_SECRET=

# ─────────────────────────────────────────────
# Vector Store
# ─────────────────────────────────────────────
VECTOR_STORE_TYPE=faiss                  # faiss or opensearch
FAISS_INDEX_PATH=./data/faiss_index
OPENSEARCH_ENDPOINT=                     # only required if VECTOR_STORE_TYPE=opensearch
OPENSEARCH_USERNAME=
OPENSEARCH_PASSWORD=

# ─────────────────────────────────────────────
# FastAPI Backend
# ─────────────────────────────────────────────
API_HOST=0.0.0.0
API_PORT=8000
API_SECRET_KEY=                          # used to sign JWT session tokens — generate with: openssl rand -hex 32

# ─────────────────────────────────────────────
# Frontend
# ─────────────────────────────────────────────
NEXT_PUBLIC_API_URL=http://localhost:8000
NEXT_PUBLIC_WS_URL=ws://localhost:8000

# ─────────────────────────────────────────────
# Safety Layer
# ─────────────────────────────────────────────
REQUIRE_CONFIRMATION_FOR_DESTRUCTIVE_ACTIONS=true   # do NOT set to false
```

---

## 2. Variable Reference Table

| Variable                                       | Required              | Default                                    | Description                                                                        |
| ---------------------------------------------- | --------------------- | ------------------------------------------ | ---------------------------------------------------------------------------------- |
| `AWS_ACCESS_KEY_ID`                            | ✅ Yes                 | —                                          | AWS IAM access key for Vega's IAM user                                             |
| `AWS_SECRET_ACCESS_KEY`                        | ✅ Yes                 | —                                          | AWS IAM secret key                                                                 |
| `AWS_REGION`                                   | ✅ Yes                 | `us-east-1`                                | Primary AWS region for all SDK calls                                               |
| `AWS_BEDROCK_REGION`                           | ✅ Yes                 | `us-east-1`                                | Region where Nova models are enabled in Bedrock                                    |
| `NOVA_LITE_MODEL_ID`                           | ✅ Yes                 | `amazon.nova-2-lite-v1:0`                  | Bedrock model ID for Nova 2 Lite (all reasoning agents)                            |
| `NOVA_SONIC_MODEL_ID`                          | ✅ Yes                 | `amazon.nova-2-sonic-v1:0`                 | Bedrock model ID for Nova 2 Sonic (voice I/O)                                      |
| `NOVA_EMBEDDING_MODEL_ID`                      | ✅ Yes                 | `amazon.nova-2-multimodal-embeddings-v1:0` | Bedrock model ID for Nova Multimodal Embeddings                                    |
| `NOVA_ACT_API_KEY`                             | ✅ Yes                 | —                                          | API key for Nova Act browser automation service                                    |
| `GITHUB_TOKEN`                                 | ✅ Yes                 | —                                          | GitHub Personal Access Token with `repo`, `issues`, `pull_requests` scopes         |
| `GITHUB_APP_CLIENT_ID`                         | ⬜ Optional            | —                                          | GitHub OAuth App client ID (only if using OAuth login flow)                        |
| `GITHUB_APP_CLIENT_SECRET`                     | ⬜ Optional            | —                                          | GitHub OAuth App client secret                                                     |
| `VECTOR_STORE_TYPE`                            | ✅ Yes                 | `faiss`                                    | Vector store backend: `faiss` (local) or `opensearch` (cloud)                      |
| `FAISS_INDEX_PATH`                             | ✅ Yes (if faiss)      | `./data/faiss_index`                       | Local filesystem path where FAISS index is stored and loaded from                  |
| `OPENSEARCH_ENDPOINT`                          | ✅ Yes (if opensearch) | —                                          | Full HTTPS endpoint for your OpenSearch domain                                     |
| `OPENSEARCH_USERNAME`                          | ✅ Yes (if opensearch) | —                                          | OpenSearch master username                                                         |
| `OPENSEARCH_PASSWORD`                          | ✅ Yes (if opensearch) | —                                          | OpenSearch master password                                                         |
| `API_HOST`                                     | ✅ Yes                 | `0.0.0.0`                                  | Interface FastAPI binds to (use `127.0.0.1` to restrict to localhost)              |
| `API_PORT`                                     | ✅ Yes                 | `8000`                                     | Port FastAPI server listens on                                                     |
| `API_SECRET_KEY`                               | ✅ Yes                 | —                                          | Secret used to sign and verify JWT session tokens — must be a strong random string |
| `NEXT_PUBLIC_API_URL`                          | ✅ Yes                 | `http://localhost:8000`                    | REST base URL exposed to the frontend                                              |
| `NEXT_PUBLIC_WS_URL`                           | ✅ Yes                 | `ws://localhost:8000`                      | WebSocket base URL exposed to the frontend                                         |
| `REQUIRE_CONFIRMATION_FOR_DESTRUCTIVE_ACTIONS` | ✅ Yes                 | `true`                                     | Safety gate toggle — must remain `true` in all environments                        |

---

## 3. IAM Policy

Vega requires a dedicated IAM user with the following minimum permissions. Do not attach `AdministratorAccess` or `PowerUserAccess` — use this scoped policy only.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "VegaCloudWatchLogs",
      "Effect": "Allow",
      "Action": [
        "logs:FilterLogEvents",
        "logs:GetLogEvents",
        "logs:DescribeLogGroups",
        "logs:DescribeLogStreams"
      ],
      "Resource": "*"
    },
    {
      "Sid": "VegaLambda",
      "Effect": "Allow",
      "Action": [
        "lambda:GetFunction",
        "lambda:ListFunctions",
        "lambda:GetFunctionConfiguration"
      ],
      "Resource": "*"
    },
    {
      "Sid": "VegaECS",
      "Effect": "Allow",
      "Action": [
        "ecs:DescribeTasks",
        "ecs:DescribeServices",
        "ecs:DescribeClusters"
      ],
      "Resource": "*"
    },
    {
      "Sid": "VegaCloudWatch",
      "Effect": "Allow",
      "Action": [
        "cloudwatch:GetMetricData",
        "cloudwatch:DescribeAlarms"
      ],
      "Resource": "*"
    },
    {
      "Sid": "VegaRDS",
      "Effect": "Allow",
      "Action": [
        "rds:DescribeDBLogFiles",
        "rds:DownloadDBLogFilePortion"
      ],
      "Resource": "*"
    },
    {
      "Sid": "VegaS3ReadOnly",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:ListBucket"
      ],
      "Resource": "*"
    },
    {
      "Sid": "VegaCodePipeline",
      "Effect": "Allow",
      "Action": [
        "codepipeline:GetPipeline",
        "codepipeline:GetPipelineExecution"
      ],
      "Resource": "*"
    },
    {
      "Sid": "VegaBedrock",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": "*"
    }
  ]
}
```

> [!NOTE] IAM Scope
> The `"Resource": "*"` entries above are intentionally broad for development convenience. For a production or shared environment, restrict resources to specific ARNs — e.g., only the CloudWatch log groups belonging to the services Vega monitors, and only the specific Bedrock model ARNs enabled in your account.

---

## 4. Setup Checklist

Work through this list top to bottom before running Vega for the first time.

- [ ] AWS account created and Amazon Bedrock Nova model access requested (Nova Lite, Nova Sonic, Nova Multimodal Embeddings)
- [ ] IAM user created (`vega-agent`) with the policy from Section 3 attached
- [ ] AWS access key generated for `vega-agent` and added to `.env`
- [ ] GitHub Personal Access Token generated with `repo`, `issues`, and `pull_requests` scopes — added to `.env` as `GITHUB_TOKEN`
- [ ] Nova Act API key obtained and added to `.env` as `NOVA_ACT_API_KEY`
- [ ] `.env` added to `.gitignore` (`echo ".env" >> .gitignore && git add .gitignore`)
- [ ] Test AWS credentials: `python -c "import boto3; boto3.client('logs').describe_log_groups(); print('AWS OK')"`
- [ ] Test Bedrock connection: `python -c "import boto3; boto3.client('bedrock', region_name='us-east-1').list_foundation_models(); print('Bedrock OK')"`
- [ ] Confirm Nova Lite model accessible: check `amazon.nova-2-lite-v1:0` appears in Bedrock model list for your region
- [ ] Confirm Nova Sonic model accessible: check `amazon.nova-2-sonic-v1:0` appears in Bedrock model list
- [ ] FAISS data directory created: `mkdir -p ./data/faiss_index`
- [ ] Run `GET /health` and verify all connections show `"connected"`
