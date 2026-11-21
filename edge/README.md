# Edge mirror

A small [Cloudflare Worker](https://developers.cloudflare.com/workers/) that serves Shellterm's packages and its first-run
download from a Cloudflare server near the phone, instead of from GitHub's release host, which can be slow on some networks.
The app uses it first and GitHub as the fallback, so nothing depends on it.

| Path | Where the bytes come from |
|---|---|
| `/apt-1` .. `/apt-4` (main packages, 9.5 GB) and `/bootstrap` | With the optional R2 bucket: a copy made by the Packages workflow (a file the bucket does not have yet comes from GitHub). Without it: the GitHub release. |
| `/apt-x11-1`, `/apt-x11-2`, `/apt-root-1` | The GitHub release. Not copied: R2's free tier holds 10 GB. |

The paths are the release download paths, `/<release tag>/<file>`. With *Workers Caching* on (it is in `wrangler.toml`),
Cloudflare keeps each answer on its edge for a day (index files for 5 minutes), so a file is fetched from R2 or GitHub once
and served from the edge afterwards, byte ranges included, which the app's parallel bootstrap download uses. `worker.js` has no
dependencies; `node --test` in this folder runs its tests.

R2 is **optional**. Without it everything still goes through Cloudflare's network, and the first download of each file comes
from GitHub; with it, every file in main is fast from the first download.

## Setup

Menu names in Cloudflare's dashboard move around now and then; the idea of each step stays the same.

### Part 1: the Worker (free, no card)

1. Create a free account at <https://dash.cloudflare.com/sign-up>.
2. Open *Workers & Pages* once, so the account has a `workers.dev` subdomain (it is asked for the first time you open it).
3. **Deploy token**: My Profile, API Tokens, Create Token, the *Edit Cloudflare Workers* template. Copy the token. Also copy
   your *Account ID* (shown on the dashboard overview).
4. In this repository, Settings, Secrets and variables, Actions, add two repository secrets: `CLOUDFLARE_API_TOKEN` (the
   token) and `CLOUDFLARE_ACCOUNT_ID` (the Account ID).
5. Actions, **Edge mirror**, *Run workflow* on `main`: it tests and deploys the Worker. Its address is
   `https://shellterm-mirror.<your subdomain>.workers.dev` (also on the Worker's page in the dashboard).
6. Check it: `curl -I https://<address>/apt-1/Packages.gz` answers `200`; asking twice, the second answer should show
   `cf-cache-status: HIT`. If the deploy is refused because of the `[cache]` setting (the account's plan does not take
   Workers Caching), add a repository *variable* (same page, Variables tab) named `EDGE_CACHE` with the value `off` and run
   it again: the Worker then only passes the files through.

### Part 2 (optional): the R2 copy of the main packages

R2 asks for a payment card before it is enabled (nothing is charged within the free tier of 10 GB of storage, 1 million
uploads and 10 million downloads a month; downloads out of R2 are free).

1. Open R2 object storage, enable it, and create a bucket (for example `shellterm-packages`).
2. In R2, Manage API tokens, create a token with *Object Read & Write* for that bucket. Copy the *Access Key ID* and the
   *Secret Access Key*.
3. Add three more repository secrets: `R2_BUCKET` (the bucket's name), `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY`.
4. Run **Edge mirror** again (it now adds the bucket to the Worker), then **Packages** on `main`: besides the GitHub releases
   it fills the bucket. The first run copies 9.5 GB, later runs replace only what changed on the weekly schedule.

Without the secrets, both workflows skip their Cloudflare steps and everything works from GitHub as before.

## Size limit

The copy never lets the bucket hold more than **9.5 GB** (a little under R2's free 10 GB). Before a lot is copied, what the
rest of the bucket holds plus the lot's size must fit; if not, nothing of that lot is copied, the run shows a warning
(*R2 copy skipped*, also in its summary) and carries on. R2 then keeps that lot's older files, which are a consistent set,
and GitHub's release stays the source of truth. The release on GitHub is published before the copy, so a skipped copy never
breaks the run or the app. If the packages grow past the limit, the warning is how you find out; either pay for more R2
storage (about $0.015 per GB a month) and raise the limit with a repository *variable* (Settings, Secrets and variables,
Actions, Variables) named `R2_MAX_BYTES` (a number of bytes), or accept that the biggest lot stays on its older copy.

## Notes

- The workflow copies the index files (`Packages*`, `Release`) after the packages and removes stale files last, so apt never
  sees an index that names a file that is not there yet.
- The free Worker plan allows 100,000 requests a day, far more than the app needs.
- If the bucket is ever empty or deleted, the Worker serves everything from GitHub; the app only loses the speed-up.
- Workers Caching is a newer Cloudflare feature (it needs wrangler 4.69 or newer, which the deploy gets with `wrangler@4`). If
  `cf-cache-status` never shows `HIT`, the Worker still works as a plain pass-through.
