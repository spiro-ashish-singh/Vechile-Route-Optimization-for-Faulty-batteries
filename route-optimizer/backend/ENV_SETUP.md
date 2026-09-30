# Required environment variables

Set these before starting the backend (`uvicorn app:app ...`). Do not
hardcode them back into source files.

| Variable | Purpose | Required |
|---|---|---|
| `TOMTOM_API_KEY` | TomTom Matrix Routing API key (live-traffic routing). Get one from the TomTom Developer Portal. | No — if unset, `/solve` automatically falls back to OSRM. |
| `BATTERY_CSV_PATH` | Path to the faulty-batteries pickup CSV. Defaults to the hardcoded path in `battery_pickups.py`. | No |
| `RWANDA_WAREHOUSE_PATH` / `UGANDA_WAREHOUSE_PATH` / `KENYA_WAREHOUSE_PATH` | Paths to each country's warehouse file. Same default-fallback behavior. | No |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` | AWS credentials for Athena (`connection.py`). Standard boto3 env vars — an AWS CLI profile or IAM role also works, nothing AWS-specific needs setting if one of those is already configured. | No — `/battery-stations` automatically falls back to the CSV export if Athena isn't reachable. |
| `AWS_REGION` | AWS region for the Athena client. Defaults to `us-east-1`. | No |
| `ATHENA_WORKGROUP` | Athena workgroup to run queries in. Defaults to `primary`. | No |
| `ATHENA_OUTPUT_S3` | S3 path for Athena query results (e.g. `s3://my-bucket/athena-results/`). Only needed if the workgroup has no default result location configured. | No |

## Rotating the TomTom key

The previous key (`2YuAfLSaDCZxrfiSC5zsFpirtzLlRU2c`) was hardcoded in
`stops_and_cost.py` in plaintext and must be treated as compromised:

1. Log into the TomTom Developer Portal.
2. Revoke/delete that key.
3. Generate a new key.
4. Set it as `TOMTOM_API_KEY` (see below) — never paste it back into a
   `.py` file.

## Setting it locally (PowerShell)

For the current terminal session only:
```powershell
$env:TOMTOM_API_KEY = "your-new-key-here"
```

To persist across sessions for your Windows user account:
```powershell
[System.Environment]::SetEnvironmentVariable("TOMTOM_API_KEY", "your-new-key-here", "User")
```
(then open a new terminal for it to take effect)

## Setting it in a deployment (Render/Railway/etc.)

Use that platform's "Environment Variables" / "Secrets" settings panel —
never bake the key into the Docker image or repo.
