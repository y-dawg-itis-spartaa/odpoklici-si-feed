# Odpoklici SI – live recall feed

Daily (and on-demand) scrape of official Slovenian recall notices:

- [GOV.SI – Nevarni in neskladni izdelki](https://www.gov.si/podrocja/podjetnistvo-in-gospodarstvo/varstvo-potrosnikov-in-konkurence/nevarni-in-neskladni-izdelki/) (food and non-food)
- [JAZMP](https://www.jazmp.si/arhiv-obvestil/) (medicines and medical devices)

Vehicles are skipped. Notices that were never placed on the Slovenian market (border / import holds) are dropped. The feed keeps a rolling **90-day** window, newest first. Risk colours are the scraper's own triage, not an official rating.

This repository is **text-only**. `recalls.json` may include an optional `image` field with the official HTTPS photo URL on `gov.si` or `jazmp.si`. Product photos are not stored or published here; the app loads them from the official hosts.

Published file: `https://<user>.github.io/<repo>/recalls.json`

No secrets are used. The workflow only needs `contents: write` (to commit) and `pages: write`.

## One-time GitHub setup

Do this on a GitHub account you control (yours or a bot account). Do not put any personal health data in the repo.

1. Create a **public** repository, for example `odpoklici-si-feed`.
2. Copy the contents of this folder to the **root** of that repository (`scrape.py`, `README.md`, `.github/workflows/daily.yml`, `.gitignore`, and `recalls.json`).
3. Push to the `main` branch.
4. In the repo: **Settings → Pages → Build and deployment**:
   - Source: **Deploy from a branch**
   - Branch: `main` / `/ (root)`
   - Save. After a minute the site is at `https://<user>.github.io/<repo>/`.
5. Confirm `https://<user>.github.io/<repo>/recalls.json` opens in a browser.
6. Tell Dr. Grok Bor the exact URL. He will put it in `www/config.js` (`DATA_URL`) and rebuild the APK. Leave the placeholder alone until this URL is live, otherwise the app would try a broken address.

## Manual run

On GitHub: **Actions → Daily recall feed → Run workflow**.

Locally (Python 3, standard library only):

```bash
python3 scrape.py --out recalls.json --days 90
```

`generated_at` is in `Europe/Ljubljana`. `schema_version` is `1`.
