#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
set -euo pipefail

# Load nvm if available (needed for correct Node version in non-login shells)
export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
if [[ -s "$NVM_DIR/nvm.sh" ]]; then
  source "$NVM_DIR/nvm.sh"
  nvm use 22 --silent 2>/dev/null || nvm use 20 --silent 2>/dev/null || true
fi

# ============================================================
# Open WebUI — AWS End-to-End Deployment Script
# ============================================================
# Uses .env as the single source of truth for all configuration.
# This sample runs the UNMODIFIED official Open WebUI image (pulled by digest
# at deploy time). There is no image build. The Amazon Bedrock integration is
# the AgentCore inference gateway (GatewayStack) plus a pipe function + two
# OpenAI connections seeded into the app database at container start.
#
# Usage:
#   ./deploy.sh                              # Full deploy (infra + image + env)
#   ./deploy.sh --env-only                   # Update env vars + restart ECS (no CDK)
#   ./deploy.sh --skip-cdk                   # Update env vars only (alias for --env-only)
#   ./deploy.sh --profile prod --region us-west-2
#   ./deploy.sh --help
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$SCRIPT_DIR/infra"
ENV_FILE="$SCRIPT_DIR/.env"
SKIP_CONFIRM=false

# Defaults (overridden by .env, then CLI args)
AWS_PROFILE=""
AWS_REGION="us-east-1"
APP_DOMAIN=""
CERTIFICATE_ARN=""
FARGATE_CPU=1024
FARGATE_MEMORY=2048
SKIP_CDK_BOOTSTRAP=false
SKIP_CDK_DEPLOY=false
ENV_ONLY=false

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

log()   { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
err()   { echo -e "${RED}[✗]${NC} $*" >&2; }
info()  { echo -e "${CYAN}[→]${NC} $*"; }
header(){ echo -e "\n${BOLD}═══ $* ═══${NC}\n"; }

usage() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Options:
  --profile NAME       AWS CLI profile name
  --region REGION      AWS region (default: us-east-1)
  --domain DOMAIN      Custom domain (e.g., oui.example.com)
  --cert-arn ARN       ACM certificate ARN for custom domain (us-east-1)
  --cpu CPU            Fargate CPU units (default: 1024)
  --memory MEM         Fargate memory MiB (default: 2048)
  --env-file FILE      Path to .env file (default: .env)
  --env-only           Update ECS env vars from .env and restart (no CDK deploy)
  --skip-bootstrap     Skip CDK bootstrap step
  --skip-cdk           Skip CDK deploy (alias for --env-only)
  --metering           Enable the opt-in metering/quota module (docs/METERING.md)
  --yes                Skip confirmation prompts
  --help               Show this help

No Docker or image build is involved — CDK deploys the unmodified official
Open WebUI image by digest and provisions the AgentCore inference gateway.
The .env file is the source of truth for application configuration;
infrastructure values (from CDK stack outputs) are auto-populated on first
deploy.
EOF
  exit 0
}

# ── Parse CLI args ──────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case $1 in
    --profile)       AWS_PROFILE="$2"; shift 2;;
    --region)        AWS_REGION="$2"; shift 2;;
    --domain)        APP_DOMAIN="$2"; shift 2;;
    --cert-arn)      CERTIFICATE_ARN="$2"; shift 2;;
    --cpu)           FARGATE_CPU="$2"; shift 2;;
    --memory)        FARGATE_MEMORY="$2"; shift 2;;
    --env-file)      ENV_FILE="$2"; shift 2;;
    --env-only)      ENV_ONLY=true; SKIP_CDK_DEPLOY=true; SKIP_CDK_BOOTSTRAP=true; shift;;
    --skip-bootstrap) SKIP_CDK_BOOTSTRAP=true; shift;;
    --skip-cdk)      ENV_ONLY=true; SKIP_CDK_DEPLOY=true; SKIP_CDK_BOOTSTRAP=true; shift;;
    --metering)      METERING=on; shift;;
    --yes)           SKIP_CONFIRM=true; shift;;
    --help)          usage;;
    *) err "Unknown option: $1"; usage;;
  esac
done

# ── Load .env file ──────────────────────────────────────────
load_env_file() {
  if [[ -f "$ENV_FILE" ]]; then
    info "Loading configuration from $ENV_FILE"
    # Read .env, skip comments and blank lines, export all vars
    while IFS='=' read -r key value; do
      # Skip comments and empty lines
      [[ -z "$key" || "$key" =~ ^[[:space:]]*# ]] && continue
      # Strip leading/trailing whitespace from key
      key=$(echo "$key" | xargs)
      # Strip surrounding quotes from value
      value=$(echo "$value" | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")
      # Only set if not already set by CLI args
      export "$key=$value" 2>/dev/null || true
    done < "$ENV_FILE"
    log "Loaded .env configuration"
  else
    warn "No .env file found at $ENV_FILE"
    if [[ -f "$SCRIPT_DIR/.env.example" ]]; then
      info "Copy .env.example to .env and configure it:"
      info "  cp .env.example .env && \$EDITOR .env"
    fi
  fi
}

load_env_file

# Override from .env if CLI didn't set them
AWS_PROFILE="${AWS_PROFILE:-${AWS_DEPLOY_PROFILE:-}}"
AWS_REGION="${AWS_REGION:-${BEDROCK_REGION:-us-east-1}}"

# ── Helpers ─────────────────────────────────────────────────
aws_cmd() {
  if [[ -n "$AWS_PROFILE" ]]; then
    aws --profile "$AWS_PROFILE" --region "$AWS_REGION" "$@"
  else
    aws --region "$AWS_REGION" "$@"
  fi
}

export_aws_credentials() {
  if [[ -n "$AWS_PROFILE" ]]; then
    local creds
    creds=$(aws --profile "$AWS_PROFILE" configure export-credentials --format env 2>/dev/null) || return 0
    eval "$creds"
  fi
}

prompt() {
  local var_name="$1" prompt_text="$2" default="$3"
  local current_val="${!var_name:-$default}"
  # Non-interactive (--yes): keep the current/default value without reading stdin.
  if [[ "$SKIP_CONFIRM" == "true" ]]; then
    eval "$var_name=\"$current_val\""
    return 0
  fi
  if [[ -n "$current_val" ]]; then
    read -rp "$(echo -e "${CYAN}?${NC}") $prompt_text [${current_val}]: " input
    eval "$var_name=\"${input:-$current_val}\""
  else
    read -rp "$(echo -e "${CYAN}?${NC}") $prompt_text: " input
    eval "$var_name=\"$input\""
  fi
}

prompt_select() {
  local var_name="$1" prompt_text="$2"
  shift 2
  local options=("$@")
  # Non-interactive (--yes): keep the current value (or first option) without stdin.
  if [[ "$SKIP_CONFIRM" == "true" ]]; then
    local current_val="${!var_name:-${options[0]}}"
    eval "$var_name=\"$current_val\""
    return 0
  fi
  echo -e "${CYAN}?${NC} $prompt_text"
  for i in "${!options[@]}"; do
    echo "  $((i+1))) ${options[$i]}"
  done
  local choice
  read -rp "  Select [1]: " choice
  choice="${choice:-1}"
  eval "$var_name=\"${options[$((choice-1))]}\""
}

confirm() {
  if [[ "$SKIP_CONFIRM" == "true" ]]; then return 0; fi
  local msg="${1:-Continue?}"
  read -rp "$(echo -e "${YELLOW}?${NC}") $msg [Y/n]: " yn
  [[ -z "$yn" || "$yn" =~ ^[Yy] ]]
}

stack_output() {
  local stack="$1" key="$2"
  aws_cmd cloudformation describe-stacks \
    --stack-name "$stack" \
    --query "Stacks[0].Outputs[?OutputKey==\`$key\`].OutputValue" \
    --output text 2>/dev/null || echo ""
}

check_command() {
  if ! command -v "$1" &>/dev/null; then
    err "$1 is required but not installed."
    exit 1
  fi
}

# ── Collect ECS environment variables from .env ─────────────
# These are the vars that get passed to the ECS task as environment overrides.
# Infrastructure vars (DATABASE_HOST, REDIS_URL, etc.) are auto-populated from stack outputs.
# Application vars from .env override defaults.
build_ecs_env_overrides() {
  local env_json="["
  local first=true

  # Vars that are safe to pass as ECS environment (not secrets)
  # Infrastructure vars are set from stack outputs; .env can override application-level vars
  local APP_VARS=(
    WEBUI_URL WEBUI_NAME
    ENABLE_OAUTH_SIGNUP OAUTH_CLIENT_ID OPENID_PROVIDER_URL OAUTH_PROVIDER_NAME
    OAUTH_SCOPES OPENID_REDIRECT_URI ENABLE_OAUTH_PERSISTENT_CONFIG
    OAUTH_USERNAME_CLAIM
    ENABLE_OAUTH_ROLE_MANAGEMENT OAUTH_ROLES_CLAIM OAUTH_ADMIN_ROLES OAUTH_ALLOWED_ROLES
    ENABLE_OAUTH_GROUP_MANAGEMENT OAUTH_GROUP_CLAIM ENABLE_OAUTH_GROUP_CREATION
    OAUTH_MERGE_ACCOUNTS_BY_EMAIL WEBUI_AUTH_SIGNOUT_REDIRECT_URL
    ENABLE_WEBSOCKET_SUPPORT
    ENABLE_OLLAMA_API OLLAMA_BASE_URLS
    ENABLE_OPENAI_API OPENAI_API_BASE_URLS OPENAI_API_KEYS
    ENABLE_SIGNUP DEFAULT_USER_ROLE WEBUI_ADMIN_EMAIL WEBUI_ADMIN_NAME
    ENABLE_RAG_WEB_SEARCH RAG_WEB_SEARCH_ENGINE
    ENABLE_IMAGE_GENERATION IMAGE_GENERATION_ENGINE
    DEFAULT_MODELS WEBUI_AUTH
    HF_HUB_OFFLINE
  )

  for var in "${APP_VARS[@]}"; do
    local val="${!var:-}"
    if [[ -n "$val" ]]; then
      if [[ "$first" == "true" ]]; then first=false; else env_json+=","; fi
      env_json+="{\"name\":\"$var\",\"value\":\"$val\"}"
    fi
  done

  env_json+="]"
  echo "$env_json"
}

# ── Preflight checks ───────────────────────────────────────
header "Preflight Checks"

# No Docker required: this sample runs the unmodified official Open WebUI
# image (pulled by digest at deploy time). The Bedrock integration is the
# AgentCore inference gateway + a pipe function seeded at container start.
for cmd in aws node npm; do
  check_command "$cmd"
  log "$cmd found: $(command -v "$cmd")"
done

if command -v cdk &>/dev/null; then
  CDK="cdk"
  log "cdk found: $(command -v cdk) ($(cdk --version 2>&1 | head -1))"
else
  warn "AWS CDK CLI not found globally, will use npx"
  CDK="npx cdk"
fi

# ── Interactive configuration (skip if --env-only) ──────────
if [[ "$ENV_ONLY" != "true" ]]; then
  header "Configuration"

  # Profile selection
  if [[ -z "$AWS_PROFILE" ]]; then
    available_profiles=$(aws configure list-profiles 2>/dev/null || echo "default")
    readarray -t profiles <<< "$available_profiles"
    if [[ ${#profiles[@]} -gt 1 ]]; then
      prompt_select AWS_PROFILE "Select AWS profile:" "${profiles[@]}"
    else
      AWS_PROFILE="${profiles[0]}"
      log "Using AWS profile: $AWS_PROFILE"
    fi
  fi

  # Region selection. The list below is examples, not exhaustive — check
  # https://docs.aws.amazon.com/general/latest/gr/bedrock.html for current
  # Bedrock regions, or pass --region explicitly.
  BEDROCK_REGIONS=("$AWS_REGION" "us-east-1" "us-east-2" "us-west-2" "eu-west-1" "ap-southeast-1" "ap-northeast-1")
  prompt_select AWS_REGION "Select AWS region (must support Bedrock; current: $AWS_REGION):" "${BEDROCK_REGIONS[@]}"

  # Domain
  prompt APP_DOMAIN "Custom domain name (leave blank to use CloudFront default domain)" ""
  if [[ -n "$APP_DOMAIN" && -z "$CERTIFICATE_ARN" ]]; then
    prompt CERTIFICATE_ARN "ACM certificate ARN for $APP_DOMAIN (must be in us-east-1)" ""
    if [[ -z "$CERTIFICATE_ARN" ]]; then
      warn "No certificate ARN provided — falling back to CloudFront default domain"
      APP_DOMAIN=""
    fi
  fi

  # Fargate sizing
  prompt FARGATE_CPU "Fargate CPU units (256/512/1024/2048/4096)" "$FARGATE_CPU"
  prompt FARGATE_MEMORY "Fargate memory MiB (512–30720)" "$FARGATE_MEMORY"
fi

# Validate credentials
info "Validating AWS credentials..."
ACCOUNT_ID=$(aws_cmd sts get-caller-identity --query Account --output text 2>/dev/null) || {
  err "Failed to validate AWS credentials for profile '$AWS_PROFILE' in region '$AWS_REGION'"
  exit 1
}
log "Authenticated as account $ACCOUNT_ID"

# Export credentials for CDK (SSO profiles need this)
export_aws_credentials

# ── Summary ─────────────────────────────────────────────────
header "Deployment Summary"

echo -e "  AWS Profile:    ${BOLD}$AWS_PROFILE${NC}"
echo -e "  AWS Region:     ${BOLD}$AWS_REGION${NC}"
echo -e "  Account ID:     ${BOLD}$ACCOUNT_ID${NC}"
echo -e "  Domain:         ${BOLD}${APP_DOMAIN:-<CloudFront default>}${NC}"
echo -e "  .env file:      ${BOLD}${ENV_FILE}${NC}"
if [[ "$ENV_ONLY" == "true" ]]; then
echo -e "  Mode:           ${BOLD}Env update only (no CDK, no build)${NC}"
else
echo -e "  Fargate CPU:    ${BOLD}$FARGATE_CPU${NC}"
echo -e "  Fargate Memory: ${BOLD}${FARGATE_MEMORY} MiB${NC}"
echo -e "  Skip Bootstrap: ${BOLD}$SKIP_CDK_BOOTSTRAP${NC}"
echo -e "  Skip CDK:       ${BOLD}$SKIP_CDK_DEPLOY${NC}"
fi
echo ""

confirm "Proceed with deployment?" || { warn "Aborted."; exit 0; }

# ── Persist deployment config for CDK ──────────────────────
DEPLOY_CONFIG="$INFRA_DIR/deploy.config.json"
info "Writing deployment config to $DEPLOY_CONFIG"
python3 -c "
import json
config = {}
try:
    with open('$DEPLOY_CONFIG') as f:
        config = json.load(f)
except: pass
domain = '$APP_DOMAIN'
cert = '$CERTIFICATE_ARN'
if domain:
    config['domainName'] = domain
elif 'domainName' in config:
    del config['domainName']
if cert:
    config['certificateArn'] = cert
elif 'certificateArn' in config:
    del config['certificateArn']
with open('$DEPLOY_CONFIG', 'w') as f:
    json.dump(config, f, indent=2)
    f.write('\n')
"
log "Deployment config saved"

# ── CDK Bootstrap ───────────────────────────────────────────
if [[ "$SKIP_CDK_BOOTSTRAP" != "true" ]]; then
  header "CDK Bootstrap"
  info "Bootstrapping CDK in $ACCOUNT_ID/$AWS_REGION..."
  cd "$INFRA_DIR"
  npm install --silent
  CDK_DEFAULT_ACCOUNT="$ACCOUNT_ID" CDK_DEFAULT_REGION="$AWS_REGION" \
    $CDK bootstrap "aws://$ACCOUNT_ID/$AWS_REGION"
  log "CDK bootstrap complete"
fi

# ── Build the metering admin console SPA (metering only) ────
# The MeteringStack ships console/dist to S3 as a deploy asset; synth fails
# with a clear message if the build is missing (docs/METERING.md).
if [[ "${METERING:-off}" == "on" && "$SKIP_CDK_DEPLOY" != "true" ]]; then
  header "Building metering admin console"
  (cd "$SCRIPT_DIR/console" && npm ci --silent && npm run build --silent) \
    && log "console built (console/dist)" \
    || { err "Metering console build failed (need node+npm; see console/README.md)."; exit 1; }
fi

# ── Vendor boto3 for the gateway provisioner Lambda ─────────
# The Lambda python3.12 runtime bundles boto3 < 1.43, which does not know the
# AgentCore "inference" gateway-target parameter. Vendor a current boto3 into
# the provisioner asset so it can create the bedrock-mantle inference target.
# (gitignored; installed here at deploy time.)
if [[ "$SKIP_CDK_DEPLOY" != "true" ]]; then
  header "Vendoring provisioner dependencies"
  PIP="python3 -m pip"; command -v pip3 >/dev/null 2>&1 && PIP="pip3"
  PROV_DIR="$SCRIPT_DIR/gateway/provisioner"
  if [[ ! -d "$PROV_DIR/boto3" ]]; then
    $PIP install --quiet --target "$PROV_DIR" -r "$PROV_DIR/requirements.txt" \
      && log "boto3 vendored into gateway/provisioner" \
      || { err "Failed to vendor boto3 (need python3 + pip)."; exit 1; }
  else
    log "provisioner boto3 already vendored"
  fi

  # The opt-in model-refresher Lambda (enableModelRefresh) also needs a current
  # boto3 + requests. Only vendor when the flag is on, so default deploys stay lean.
  if [[ "$(cd "$INFRA_DIR" && npx --no-install cdk context 2>/dev/null | grep -c 'enableModelRefresh.*true')" != "0" \
        || "${ENABLE_MODEL_REFRESH:-false}" == "true" ]]; then
    REFR_DIR="$SCRIPT_DIR/gateway/refresher"
    if [[ ! -d "$REFR_DIR/boto3" ]]; then
      $PIP install --quiet --target "$REFR_DIR" -r "$REFR_DIR/requirements.txt" \
        && log "boto3 + requests vendored into gateway/refresher" \
        || { err "Failed to vendor refresher deps (need python3 + pip)."; exit 1; }
    else
      log "refresher deps already vendored"
    fi
  fi
fi

# ── CDK Deploy ──────────────────────────────────────────────
if [[ "$SKIP_CDK_DEPLOY" != "true" ]]; then
  header "CDK Deploy"
  cd "$INFRA_DIR"
  npm install --silent

  if [[ "${METERING:-off}" == "on" ]]; then
    info "Deploying all stacks (Network → Data → Auth → Gateway → Compute → Metering)..."
  else
    info "Deploying all stacks (Network → Data → Auth → Gateway → Compute)..."
  fi
  info "No image build — ECS pulls the unmodified official Open WebUI image by digest."
  # The model-refresher Lambda is enabled by the CDK context flag
  # `enableModelRefresh`. Forward it from the ENABLE_MODEL_REFRESH env var (.env
  # or shell) so the same switch that vendored its deps above also deploys it —
  # otherwise the env var would vendor but never turn the Lambda on. Optional
  # cadence via MODEL_REFRESH_RATE_HOURS (default 24 in the stack).
  REFRESH_CTX=()
  if [[ "${ENABLE_MODEL_REFRESH:-false}" == "true" ]]; then
    REFRESH_CTX+=(-c "enableModelRefresh=true")
    [[ -n "${MODEL_REFRESH_RATE_HOURS:-}" ]] && REFRESH_CTX+=(-c "modelRefreshRateHours=${MODEL_REFRESH_RATE_HOURS}")
    info "Model-capability refresher: ENABLED (enableModelRefresh=true)"
  fi

  # Open WebUI image: configurable via .env OPEN_WEBUI_IMAGE; defaults to latest.
  OWUI_IMAGE="${OPEN_WEBUI_IMAGE:-ghcr.io/open-webui/open-webui:latest}"
  IMAGE_CTX=(-c "openWebuiImage=${OWUI_IMAGE}")
  info "Open WebUI image: $OWUI_IMAGE"

  CDK_DEFAULT_ACCOUNT="$ACCOUNT_ID" CDK_DEFAULT_REGION="$AWS_REGION" \
    $CDK deploy --all -c "metering=${METERING:-off}" "${REFRESH_CTX[@]}" "${IMAGE_CTX[@]}" --require-approval "$([ "$SKIP_CONFIRM" = "true" ] && echo never || echo broadening)"
  log "CDK deploy complete"
fi

# ── Collect stack outputs ───────────────────────────────────
header "Collecting Stack Outputs"

CF_DOMAIN=$(stack_output "OpenWebUI-Compute" "DistributionDomainName")
CF_ID=$(stack_output "OpenWebUI-Compute" "DistributionId")
APP_URL=$(stack_output "OpenWebUI-Compute" "AppUrl")
SERVICE_NAME=$(stack_output "OpenWebUI-Compute" "ServiceName")
APP_IMAGE_URI=$(stack_output "OpenWebUI-Compute" "AppImageUri")
USER_POOL_ID=$(stack_output "OpenWebUI-Auth" "UserPoolId")
CLIENT_ID=$(stack_output "OpenWebUI-Auth" "UserPoolClientId")
COGNITO_DOMAIN_OUT=$(stack_output "OpenWebUI-Auth" "CognitoDomain")

log "App URL:           $APP_URL"
log "CloudFront Domain: $CF_DOMAIN"
log "ECS Service:       $SERVICE_NAME"

# ── Auto-populate .env with infrastructure values ───────────
header "Updating .env with Infrastructure Values"

# Create .env from template if it doesn't exist
if [[ ! -f "$ENV_FILE" ]]; then
  if [[ -f "$SCRIPT_DIR/.env.example" ]]; then
    cp "$SCRIPT_DIR/.env.example" "$ENV_FILE"
    info "Created .env from .env.example"
  else
    touch "$ENV_FILE"
  fi
fi

# Update infrastructure values in .env (these come from CDK stack outputs)
update_env_var() {
  local key="$1" value="$2"
  if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
  else
    echo "${key}=${value}" >> "$ENV_FILE"
  fi
}

update_env_var "WEBUI_URL" "$APP_URL"
update_env_var "OAUTH_CLIENT_ID" "$CLIENT_ID"
update_env_var "OPENID_PROVIDER_URL" "https://cognito-idp.${AWS_REGION}.amazonaws.com/${USER_POOL_ID}/.well-known/openid-configuration"
update_env_var "OPENID_REDIRECT_URI" "${APP_URL}/oauth/oidc/callback"
update_env_var "OAUTH_PROVIDER_NAME" "Amazon Cognito"
update_env_var "ENABLE_OAUTH_SIGNUP" "true"
update_env_var "DEFAULT_USER_ROLE" "pending"
update_env_var "OAUTH_SCOPES" "openid email profile"
update_env_var "ENABLE_OAUTH_PERSISTENT_CONFIG" "false"
update_env_var "OAUTH_USERNAME_CLAIM" "email"
update_env_var "ENABLE_OAUTH_ROLE_MANAGEMENT" "true"
update_env_var "OAUTH_ROLES_CLAIM" "cognito:groups"
update_env_var "OAUTH_ADMIN_ROLES" "admin,webui-admins,admins"
update_env_var "OAUTH_ALLOWED_ROLES" "admin,webui-admins,admins,user,power-users,basic-users"
update_env_var "ENABLE_OAUTH_GROUP_MANAGEMENT" "true"
update_env_var "OAUTH_GROUP_CLAIM" "cognito:groups"
update_env_var "ENABLE_OAUTH_GROUP_CREATION" "true"
update_env_var "OAUTH_MERGE_ACCOUNTS_BY_EMAIL" "true"
update_env_var "WEBUI_AUTH_SIGNOUT_REDIRECT_URL" "https://${COGNITO_DOMAIN_OUT}/logout?client_id=${CLIENT_ID}&logout_uri=${APP_URL}/auth"
update_env_var "S3_BUCKET_NAME" "$(stack_output 'OpenWebUI-Data' 'S3BucketName')"
update_env_var "S3_REGION_NAME" "$AWS_REGION"

# Reload .env after updates
load_env_file

log ".env updated with infrastructure values"

# ── Sync Cognito client secret to Secrets Manager ──────────
header "Syncing Cognito Client Secret"

CLIENT_SECRET=$(aws_cmd cognito-idp describe-user-pool-client \
  --user-pool-id "$USER_POOL_ID" \
  --client-id "$CLIENT_ID" \
  --query 'UserPoolClient.ClientSecret' \
  --output text)

if [[ -n "$CLIENT_SECRET" && "$CLIENT_SECRET" != "None" ]]; then
  aws_cmd secretsmanager put-secret-value \
    --secret-id "open-webui/cognito-client-secret" \
    --secret-string "$CLIENT_SECRET" >/dev/null
  log "Cognito client secret synced to Secrets Manager"
else
  warn "No client secret found"
fi

# ── Update ECS environment variables from .env ──────────────
header "Updating ECS Environment Variables"

info "Building environment overrides from .env..."
ENV_OVERRIDES=$(build_ecs_env_overrides)
ENV_COUNT=$(echo "$ENV_OVERRIDES" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")
log "Found $ENV_COUNT application env vars to set"

# Get current task definition and create new revision with updated env vars
TASK_DEF_ARN=$(aws_cmd ecs describe-services \
  --cluster open-webui-cluster \
  --services "$SERVICE_NAME" \
  --query 'services[0].taskDefinition' --output text)

info "Current task definition: $TASK_DEF_ARN"

# Get the task def, merge env vars, register new revision
aws_cmd ecs describe-task-definition --task-definition "$TASK_DEF_ARN" \
  --query 'taskDefinition' --output json > /tmp/taskdef.json

python3 -c "
import json, sys

with open('/tmp/taskdef.json') as f:
    td = json.load(f)

# Merge .env overrides into container environment
env_overrides = json.loads('''$ENV_OVERRIDES''')
override_keys = {e['name'] for e in env_overrides}

# Deprecated vars to remove from previous task definitions
deprecated_vars = {
    'ENABLE_COGNITO_AUTH', 'COGNITO_USER_POOL_ID', 'COGNITO_CLIENT_ID',
    'COGNITO_DOMAIN', 'COGNITO_REGION', 'COGNITO_REDIRECT_URI',
    'BEDROCK_GROUP_MODEL_ACCESS', 'BEDROCK_ENFORCE_GROUP_ACCESS',
    'BEDROCK_USER_TOKEN_QUOTA_ENABLED', 'BEDROCK_USER_DAILY_TOKEN_LIMIT',
    'BEDROCK_USER_MONTHLY_TOKEN_LIMIT', 'BEDROCK_GROUP_TOKEN_QUOTAS',
    'BEDROCK_MODEL_PRICING', 'BEDROCK_USAGE_RETENTION_DAYS',
}

container = td['containerDefinitions'][0]
existing_env = [e for e in container.get('environment', [])
                if e['name'] not in override_keys and e['name'] not in deprecated_vars]
container['environment'] = existing_env + env_overrides

# Keep only fields needed for register-task-definition
keep_fields = [
    'family', 'taskRoleArn', 'executionRoleArn', 'networkMode',
    'containerDefinitions', 'volumes', 'placementConstraints',
    'requiresCompatibilities', 'cpu', 'memory', 'runtimePlatform',
]
new_td = {k: td[k] for k in keep_fields if k in td}

with open('/tmp/taskdef-new.json', 'w') as f:
    json.dump(new_td, f)
"

NEW_TASK_DEF_ARN=$(aws_cmd ecs register-task-definition \
  --cli-input-json file:///tmp/taskdef-new.json \
  --query 'taskDefinition.taskDefinitionArn' --output text)

log "Registered new task definition: $NEW_TASK_DEF_ARN"

# ── Update Cognito callback URLs ────────────────────────────
header "Updating Cognito Callback URLs"

ACTUAL_CALLBACK="${APP_URL}/oauth/oidc/callback"
ACTUAL_LOGOUT="${APP_URL}/auth"

aws_cmd cognito-idp update-user-pool-client \
  --user-pool-id "$USER_POOL_ID" \
  --client-id "$CLIENT_ID" \
  --callback-urls "$ACTUAL_CALLBACK" \
  --logout-urls "$ACTUAL_LOGOUT" \
  --allowed-o-auth-flows "code" \
  --allowed-o-auth-scopes "openid" "email" "profile" \
  --allowed-o-auth-flows-user-pool-client \
  --supported-identity-providers "COGNITO" >/dev/null
log "Cognito callback URL updated to OIDC path"

# ── Deploy new task definition to ECS ───────────────────────
header "Deploying to ECS"

info "Updating service with new task definition..."
aws_cmd ecs update-service \
  --cluster open-webui-cluster \
  --service "$SERVICE_NAME" \
  --task-definition "$NEW_TASK_DEF_ARN" \
  --force-new-deployment >/dev/null
log "ECS deployment triggered"

info "Waiting for service to stabilize..."
aws_cmd ecs wait services-stable \
  --cluster open-webui-cluster \
  --services "$SERVICE_NAME" 2>/dev/null && \
  log "ECS service is stable" || \
  warn "Timed out waiting — check ECS console for status"

# Clean up
rm -f /tmp/taskdef.json /tmp/taskdef-new.json

# ── Done ────────────────────────────────────────────────────
header "Deployment Complete"

echo -e "  ${GREEN}Application URL:${NC}    ${BOLD}$APP_URL${NC}"
echo -e "  ${GREEN}CloudFront Domain:${NC}  ${BOLD}$CF_DOMAIN${NC}"
echo -e "  ${GREEN}CloudFront ID:${NC}      ${BOLD}$CF_ID${NC}"
echo -e "  ${GREEN}Cognito Domain:${NC}     ${BOLD}https://$COGNITO_DOMAIN_OUT (Managed Login)${NC}"
echo -e "  ${GREEN}ECS Cluster:${NC}        ${BOLD}open-webui-cluster${NC}"
echo -e "  ${GREEN}ECS Service:${NC}        ${BOLD}$SERVICE_NAME${NC}"
if [[ -n "$APP_IMAGE_URI" ]]; then
echo -e "  ${GREEN}Image (official):${NC}   ${BOLD}$APP_IMAGE_URI${NC}"
fi
echo ""
echo -e "  ${CYAN}Quick commands:${NC}"
echo -e "    Update env vars only:    ${BOLD}./deploy.sh --env-only --profile $AWS_PROFILE${NC}"
echo -e "    Redeploy (infra+image):  ${BOLD}./deploy.sh --skip-bootstrap --profile $AWS_PROFILE${NC}"
echo -e "    Full redeploy:           ${BOLD}./deploy.sh --profile $AWS_PROFILE${NC}"
if [[ -n "$APP_DOMAIN" ]]; then
  echo -e "    DNS CNAME:        ${BOLD}$APP_DOMAIN → $CF_DOMAIN${NC}"
fi
echo ""
log "Done! 🚀"
