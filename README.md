# SU STORE Order API

**English** · [ภาษาไทย](README.th.md)

## What this is

The backend for the SU STORE shop ([sumfu.store](https://sumfu.store)). It is a Python HTTP server built on the standard library's `http.server` and SQLite. It serves the store API under `/v2/*` and a few public routes: `/health`, `/site-status` and `/product-images/`. The Next.js storefront lives in [SU-STORE-REMASTER](https://github.com/Weerapong-gui/SU-STORE-REMASTER) and calls this API through its own proxy.

## Run it locally

You need Docker. From the repo root:

```sh
./dev.sh
```

The API starts at `http://127.0.0.1:10000` on a sample database in `data/dev/` (not real data), with sample products, categories, a home page and orders.

- Back office login: `staff` / `dev`
- API token: `dev-token`
- `./dev.sh reset` wipes the sample database and starts over.

## Checks

Use Python 3.11 or newer. Create a virtual environment, install `pytest`, `ruff==0.16.10` and `pyyaml==6.0.2`, then run:

```sh
.venv/bin/ruff check .
ORDER_API_DB_PATH=/tmp/su-order-api/orders.db ORDER_API_SLIPS_DIR=/tmp/su-order-api/slips .venv/bin/python -m pytest tests/ -v
```

## Layout

```text
order_api.py   entry point, starts the HTTP server
core/          config, database, HTTP handler, public routes, slips
store/         store API /v2/*: products, orders, receipts, staff, home page
contract/      shared contract with the storefront (constants, receipt template, OpenAPI)
ops/           production scripts: deploy, backup, migration
tests/         pytest
```

## Contract

`contract/` is the source of truth for the contract between the backend and the storefront. The storefront pulls it from this repo's `main` branch and keeps a copy in its own repo. Tests here check that the Python code uses the same values as `contract/`.

Compatibility rule:

> The backend must always support the storefront that is currently deployed. It may add things, but it may remove a field or endpoint, or change what one means, only after the storefront has stopped using it. For work that touches both sides, merge the backend first.

## Deploy

Merging into `main` deploys to production. `.github/workflows/deploy.yml` runs the tests, builds a multi-arch image, pushes it to GHCR (tagged with the commit SHA), then `ops/deploy-service.sh` on the production host pulls it and restarts only the API. If the new container fails its health check, the previous version is restored.

To roll back, run the deploy workflow with an earlier commit: `gh workflow run deploy.yml -f sha=<commit sha>`.

Host details, secrets and data paths are kept off this public repo.

## Backup

Every day at 04:00 (Thailand time), the systemd timer `su-store-backup.timer` on the production host runs `ops/backup.sh`. It takes a `sqlite3 .backup()` of the database inside the `su-store-api` container into `data/order-api/backups/` and checks it with `PRAGMA integrity_check`. On success it writes `LAST_OK` and keeps 14 days of backups. A second machine pulls the backups, slips and product images every day and never deletes anything on its side. If a backup fails, look at `journalctl -u su-store-backup`.
