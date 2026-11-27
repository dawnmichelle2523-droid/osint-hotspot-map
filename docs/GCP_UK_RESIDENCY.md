# Google Cloud data residency — London and Virginia

GeoRiskIntel can run as a Cloud Run service so a government or public-sector tenant keeps the application in-country. The application does not copy its SQLite database to another region. You choose the region at deploy time.

| Tenant | Cloud Run region | Place |
| --- | --- | --- |
| UK and EU public sector | `europe-west2` | London |
| US public sector | `us-east4` | Northern Virginia |

Do not use a multi-region location (`eu`, `us`, or `europe`) for the service or for the volume that holds the database.

TLS 1.3 is terminated by the Google Front End. The app sends HSTS when it sees HTTPS or `X-Forwarded-Proto: https`. Set `TRUST_PROXY=true` on Cloud Run so the rate limiter sees the client address the front end recorded, not the shared proxy address.

## What you need

- A Google Cloud project you are allowed to deploy.
- `gcloud` installed and authenticated (`gcloud auth login` and `gcloud config set project PROJECT_ID`).
- This repository checked out. `requirements.txt` and the `Procfile` are what the Python buildpack uses.
- Secret Manager secrets for any live credential. Do not put secret values in the shell history or in `--set-env-vars`.

The commands below are the 15-minute path: enable APIs, create secrets, deploy, then check the region and the public security documents. The first Cloud Build often takes longer than the typing.

## 1. Enable the APIs

UK:

```bash
export PROJECT_ID="your-project-id"
export REGION="europe-west2"
gcloud config set project "${PROJECT_ID}"
gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com secretmanager.googleapis.com --project "${PROJECT_ID}"
```

US tenants use the same commands with `REGION=us-east4`.

## 2. Put secrets in Secret Manager

Create these only for values you actually use. Leave a secret out of `--set-secrets` if you do not have it.

```bash
printf '%s' 'replace-me' | gcloud secrets create XAI_API_KEY --data-file=- --replication-policy=user-managed --locations="${REGION}"
printf '%s' 'replace-me' | gcloud secrets create PPV_SECRET --data-file=- --replication-policy=user-managed --locations="${REGION}"
printf '%s' 'replace-me' | gcloud secrets create PREVIEW_PASSWORD --data-file=- --replication-policy=user-managed --locations="${REGION}"
```

`--replication-policy=user-managed --locations` keeps the secret in London or Virginia. Give the Cloud Run runtime service account `roles/secretmanager.secretAccessor` on each secret.

## 3. Deploy into the region

From the repository root. Replace `https://YOUR_DOMAIN` with the public origin. That value is the `Canonical` URL in `security.txt`.

```bash
gcloud run deploy georiskintel \
  --source . \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --platform managed \
  --execution-environment gen2 \
  --allow-unauthenticated \
  --set-env-vars "TRUST_PROXY=true,PUBLIC_BASE_URL=https://YOUR_DOMAIN" \
  --set-secrets "XAI_API_KEY=XAI_API_KEY:latest,PPV_SECRET=PPV_SECRET:latest,PREVIEW_PASSWORD=PREVIEW_PASSWORD:latest"
```

`--source` builds with Cloud Build and deploys that image to Cloud Run in `${REGION}`. For a US tenant, `${REGION}` is `us-east4`. For a UK tenant it is `europe-west2`.

`--allow-unauthenticated` lets the Google Front End reach the app. The preview password, when set, still gates the product. Omit `PREVIEW_PASSWORD` from `--set-secrets` when the site should be public.

To deploy only one of the regions, run the command once with that region. A second project, or a second service name, is how you run London and Virginia side by side. One service has one region.

## 4. Confirm it landed in-country

```bash
gcloud run services describe georiskintel \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --format='yaml(metadata.name,status.url,status.traffic)'
```

The service resource must list the region you passed. Then, with the service URL:

```bash
curl -sI "https://SERVICE_URL/api/health/security"
curl -s "https://SERVICE_URL/api/health/security"
curl -s "https://SERVICE_URL/.well-known/security.txt"
```

On HTTPS the first response includes `strict-transport-security: max-age=31536000; includeSubDomains`. The JSON posture check returns booleans only.

## Durable database in the same region

Cloud Run's own disk is ephemeral. A new revision can drop a SQLite file written inside the container. For a pilot, that is enough to prove the region. Before the tenant stores operational incident data:

1. Create a Filestore instance (or another NFS volume) in the **same** region, `europe-west2` or `us-east4`.
2. Reach it over a Serverless VPC Access connector in that region.
3. Mount it at `/data` and set `DATA_DIR=/data`.

```bash
gcloud run services update georiskintel \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --vpc-connector "PROJECT_NUMBER-connector" \
  --add-volume "name=data,type=nfs,location=10.x.x.x:/georisk" \
  --add-volume-mount "volume=data,mount-path=/data" \
  --update-env-vars "DATA_DIR=/data,TRUST_PROXY=true"
```

Confirm the flags against `gcloud run services update --help` if the CLI on your machine is older. Do not point `DATA_DIR` at a multi-region Cloud Storage bucket. SQLite on a multi-region bucket is both a residency break and an unsafe file lock.

## Private access

A public-sector tenant that must not expose the service on the internet can front it with Identity-Aware Proxy or restrict ingress:

```bash
gcloud run services update georiskintel \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --ingress internal-and-cloud-load-balancing
```

That is optional. The 15-minute path above uses public ingress plus the application's own preview control.

## What not to do

- Do not pass secret values with `--set-env-vars` or commit a filled `.env`.
- Do not deploy the service to `europe-west1` or `us-central1` and call it London or Virginia. The residency region is `europe-west2` or `us-east4`.
- Do not replicate the SQLite file to a second region "for backup" unless the tenant has agreed that copy in writing. A backup has to stay in the same residency boundary.
