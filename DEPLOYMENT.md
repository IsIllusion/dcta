# DCTA public deployment

## Selected option: Render Free

The public edition uses Render's free Docker web service in Singapore. Render supplies the HTTPS address and certificate. The project continues to use Tencent ADP, Tencent TRTC and the independent DeepSeek checker.

The free service sleeps after 15 minutes without incoming traffic. The next visit takes about a minute to wake it. Visitor history and balances reset when the instance sleeps, restarts or redeploys. Each awake instance still isolates visitors from one another. Cloud AI and speech usage is billed by the existing providers separately from hosting.

### Initial setup

1. Sign in to Render and connect a GitHub account.
2. Create a private repository containing the files from the source-only deployment archive. Upload the extracted files at the repository root, including `render.yaml` and `Dockerfile`.
3. In Render, choose **New > Blueprint** and select that repository.
4. Confirm **Free**, **Singapore**, one web service, and no database or disk add-ons.
5. Set the seven provider environment variables requested by the Blueprint. Values are available in the local private `.env`; they are excluded from the repository and archive.
6. Deploy. Render provides `RENDER_EXTERNAL_URL`; the start script uses it for host, origin and device-verification settings. No purchased domain is required.
7. Open the assigned HTTPS URL in two separate browser profiles. Check separate histories, voice transcription, signed simulated payments and receipts. Complete another payment in the same conversation.

The Blueprint disables automatic redeployment on commits. Deploy a reviewed version manually. Initial launch refuses a populated data directory, and the image contains only source code, templates and frontend assets. Local test records cannot enter the deployment through the release archive.

### Free-instance settings

| Setting | Value |
| --- | --- |
| Visitor workspaces | 12 |
| Concurrent HTTP requests | 4 |
| Concurrent speech recognition jobs | 1 |
| AI-related HTTP requests per visitor | 60 |
| AI-related HTTP requests per UTC day | 300 |
| Visitor session expiry | 24 hours, or earlier if the host restarts |

Request counters are stored with the temporary visitor data and reset with the instance. One application request can involve multiple provider calls. These limits are request limits, not monetary spending limits.

## Alternative: a persistent Linux server

The existing Compose deployment is available if persistent demo history is needed later:

1. Point an owned domain at the Linux server and allow TCP 80/443.
2. Place the extracted release in its own directory. Transfer `.env` separately and set file permissions to `600`.
3. Set `DEMO_DOMAIN`, the matching `PUBLIC_ORIGIN`, and a unique `DEPLOYMENT_SLOT` for the first public launch. Use a different slot for staging.
4. Build with `docker compose build`.
5. Check the new volume with `docker compose run --rm --no-deps web python -m dcta.deployment_check --fresh-data`. This check makes no provider calls and refuses a populated volume.
6. Run `docker compose up -d`.

Use the same deployment slot for later updates to preserve visitor records. Do not delete a volume as part of a normal update. Caddy manages HTTPS; the app's internal port is not published.

## Release preparation

Run `python tools/build_release.py` from the public-edition source checkout. The archive has a file manifest and a separate SHA-256 checksum. Its file allowlist excludes `.env`, databases, recordings, screenshots, test outputs, submission documents and the local `works` directory.

## References

- [Render free service behavior](https://render.com/docs/free)
- [Render Blueprint configuration](https://render.com/docs/blueprint-spec)
- [Render runtime environment](https://render.com/docs/environment-variables)
