# ad-sync

Automatically syncs audio descriptions from [AudioVault](https://audiovault.net) with TV episodes and movies downloaded by [Sonarr](https://sonarr.tv) and [Radarr](https://radarr.video).

When Sonarr or Radarr imports a new file, ad-sync:

1. Searches AudioVault for a matching audio description track.
2. Downloads the file (caching season ZIPs so they are only fetched once per season).
3. Runs [describealign](https://github.com/julbean/describealign) to align and combine the audio description with the video.
4. Keeps the combined file only if the alignment score is **65 % or above**; otherwise the audio description is discarded without touching the original video.

The combined file is placed alongside the original in the same directory, prefixed with `ad_`.

---

## Requirements

- Python 3.10 or later
- [describealign](https://github.com/julbean/describealign) (`pip install describealign`)
- An AudioVault account (<https://audiovault.net/register>)
- Sonarr v3+ and/or Radarr v3+

---

## Installation

```bash
pip install git+https://github.com/matalvernaz/ad-sync.git
```

Or clone and install locally:

```bash
git clone https://github.com/matalvernaz/ad-sync.git
cd ad-sync
pip install .
```

---

## Configuration

Copy the example config file and fill in your AudioVault credentials:

```bash
mkdir -p ~/.config/ad-sync
cp .env.example ~/.config/ad-sync/.env
nano ~/.config/ad-sync/.env   # or any editor you prefer
```

The config file looks like this:

```env
AUDIOVAULT_EMAIL=your@email.com
AUDIOVAULT_PASSWORD=yourpassword

# Minimum alignment score to keep a combined file (default: 65)
AD_SYNC_MIN_SCORE=65

# Optional: override the cache directory (default: ~/.cache/ad-sync)
# AD_SYNC_CACHE_DIR=/path/to/cache
```

> **Security note:** Keep this file private (`chmod 600 ~/.config/ad-sync/.env`). It contains your AudioVault password.

### Verify your credentials

```bash
python -m ad_sync --test-auth
```

---

## Setting up in Sonarr

1. Open Sonarr → **Settings → Connect → + (Add Connection)**.
2. Choose **Custom Script**.
3. Fill in:
   - **Name:** `ad-sync`
   - **On Import:** ✅ enabled
   - **On Upgrade:** ✅ enabled
   - **Path:** output of `which ad-sync` (e.g. `/usr/local/bin/ad-sync`)
4. Click **Test** — you should see a green tick.
5. Click **Save**.

Sonarr will now call ad-sync every time it imports or upgrades a file.

---

## Setting up in Radarr

The steps are identical to Sonarr:

1. Open Radarr → **Settings → Connect → + (Add Connection)**.
2. Choose **Custom Script**.
3. Fill in:
   - **Name:** `ad-sync`
   - **On Import:** ✅ enabled
   - **On Upgrade:** ✅ enabled
   - **Path:** output of `which ad-sync`
4. Click **Test**, then **Save**.

---

## How it works

### TV episodes (Sonarr)

AudioVault distributes audio descriptions for TV shows as ZIP files containing one MP3 per episode. ad-sync:

1. Searches AudioVault for the series name and season number.
2. Downloads the season ZIP (cached — only downloaded once per season).
3. Extracts the ZIP and finds the right episode MP3 by episode number.
4. Runs describealign on the video + MP3.
5. Checks the alignment score; keeps the combined file if ≥ 65 %.

### Movies (Radarr)

AudioVault distributes movie audio descriptions as individual MP3 files. ad-sync searches by title, downloads the file, and runs the same alignment + scoring step.

### Caching

Downloaded season ZIPs are stored in `~/.cache/ad-sync/shows/<series>/` and are never re-downloaded for the same season. A `manifest.json` in each cache directory tracks which URLs have been downloaded.

---

## Troubleshooting

**"Login failed"** — Double-check your credentials in `~/.config/ad-sync/.env` and run `python -m ad_sync --test-auth`.

**"No AudioVault results"** — The show or movie may not have an audio description on AudioVault yet.

**"Score X% is below threshold"** — The audio description didn't align well with the video (possibly a different version/cut). The original video is untouched.

**describealign not found** — Install it: `pip install describealign`.

---

## Adjusting the score threshold

If you want to accept lower-quality alignments, set `AD_SYNC_MIN_SCORE=50` in your `.env`. The describealign documentation notes that scores below 20 % are likely mismatched files, and scores above 90 % may indicate undescribed media.

---

## License

MIT
