#!/usr/bin/env bash
# Create the GCP project Terraform will deploy into.
#
#   scripts/gcp_bootstrap_project.sh PROJECT_ID BILLING_ACCOUNT
#
# BILLING_ACCOUNT is the id from `gcloud billing accounts list` (e.g. 0X0X0X-0X0X0X-0X0X0X).
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 PROJECT_ID BILLING_ACCOUNT" >&2
  exit 2
fi

project_id="$1"
billing_account="$2"

services=(
  compute.googleapis.com
  cloudkms.googleapis.com
  servicedirectory.googleapis.com
  storage.googleapis.com
  iap.googleapis.com
  iam.googleapis.com
  iamcredentials.googleapis.com
  cloudresourcemanager.googleapis.com
  oslogin.googleapis.com
  logging.googleapis.com
)

if gcloud projects describe "$project_id" >/dev/null 2>&1; then
  echo "project $project_id already exists, reusing it"
else
  gcloud projects create "$project_id"
fi

billing_enabled=$(gcloud billing projects describe "$project_id" --format='value(billingEnabled)' 2>/dev/null || true)
if [ "$billing_enabled" = "True" ]; then
  echo "project $project_id is already billing-enabled, skipping billing link"
else
  gcloud billing projects link "$project_id" --billing-account "$billing_account"
fi

gcloud services enable "${services[@]}" --project "$project_id"
gcloud config set project "$project_id"

echo
echo "project $project_id is ready. Next:"
echo "  scripts/build_dist.sh"
echo "  cp terraform/terraform.tfvars.example terraform/terraform.tfvars   # set project_id + operator_emails"
echo "  terraform -chdir=terraform init && terraform -chdir=terraform apply"
