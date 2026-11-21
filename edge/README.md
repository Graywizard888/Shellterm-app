# Edge mirror

A small [Cloudflare Worker](https://developers.cloudflare.com/workers/) that serves Shellterm's packages and its first-run
download from a Cloudflare server near the phone, instead of from GitHub's release host, which can be slow on some networks.
The app uses it first and GitHub as the fallback, so nothing depends on it.

| Path | Where the bytes come from |
|---|---|
| `/apt-1` .. `/apt-4` (main packages, 9.5 GB) and `/bootstrap` | A copy in a Cloudflare R2 bucket, made by the Packages workflow. A file the bucket does not have yet is fetched from the GitHub release. |
| `/apt-x11-1`, `/apt-x11-2`, `/apt-root-1` | Fetched from the GitHub release once, then kept on Cloudflare's edge for a day (index files for 5 minutes). Not copied: R2's free tier holds 10 GB. |

The paths are the release download paths, `/<release tag>/<file>`. Byte ranges are supported, which the app's parallel
bootstrap download uses. `worker.js` has no dependencies; `node --test` in this folder runs its tests.

## One-time setup

Menu names in Cloudflare's dashboard move around now and then; the idea of each step stays the same.

1. Create a free account at <https://dash.cloudflare.com/sign-up>.
2. **R2**: open R2 object storage and enable it (this asks for a payment card; nothing is charged within the free tier of
   10 GB of storage, 1 million uploads and 10 million downloads a month, and downloads out of R2 are free). Create a bucket
   named `shellterm-packages`.
3. **R2 API token**: in R2, Manage API tokens, create one with *Object Read & Write* for that bucket. Copy the *Access Key ID*
   and *Secret Access Key*. The *Account ID* is shown on the same page and on the dashboard's overview.
4. **Deploy token**: My Profile, API Tokens, Create Token, the *Edit Cloudflare Workers* template. Copy the token.
5. Open *Workers & Pages* once, so the account has a `workers.dev` subdomain (it is asked for the first time you open it).
6. In this repository, Settings, Secrets and variables, Actions, add five repository secrets:

   | Secret | Value |
   |---|---|
   | `R2_ACCOUNT_ID` | the Account ID |
   | `R2_BUCKET` | `shellterm-packages` |
   | `R2_ACCESS_KEY_ID` | the R2 token's Access Key ID |
   | `R2_SECRET_ACCESS_KEY` | the R2 token's Secret Access Key |
   | `CLOUDFLARE_API_TOKEN` | the deploy token |

7. Actions, **Edge mirror**, *Run workflow* on `main`: it tests and deploys the Worker. Its address is
   `https://shellterm-mirror.<your subdomain>.workers.dev` (shown in the Worker's page in the dashboard).
8. Actions, **Packages**, *Run workflow* on `main`: besides the GitHub releases it now fills the bucket. The first run copies
   9.5 GB, later runs replace only what changed on the weekly schedule.
9. Check it: `curl -I https://<address>/apt-1/Packages.gz` answers `200`.

Without the secrets, both workflows skip their Cloudflare steps and everything works from GitHub as before.

## Notes

- The workflow copies the index files (`Packages*`, `Release`) after the packages and removes stale files last, so apt never
  sees an index that names a file that is not there yet.
- The free Worker plan allows 100,000 requests a day, far more than the app needs.
- If the bucket is ever empty or deleted, the Worker serves everything from GitHub; the app only loses the speed-up.
