# describarr

Automatically syncs audio descriptions from [AudioVault](https://audiovault.net) with TV episodes and movies downloaded by [Sonarr](https://sonarr.tv) and [Radarr](https://radarr.video).

When Sonarr or Radarr imports a new file, describarr:

1. Searches AudioVault for a matching audio description track.
2. Downloads the file (caching season ZIPs so they are only fetched once per season).
3. Runs [describealaign](https://github.com/matalvernaz/describealaign) to align and combine the audio description with the video.
4. If the alignment is confident enough that the description lands at the right time, replaces the original file in-place with the combined version. The primary gate is the **similarity score (default ≥ 65 %)**, which measures how well the description track's embedded program audio locked onto the video — i.e. how confidently the narration was placed in time. A secondary *drift rescue* accepts a lower similarity only when the time-mapping is uniform and the cause is known (commercial-break seams, or a PAL/NTSC rate conversion). Two narrower rescues each cover one more measured cause: a *mixed-rate rescue* for off-air recordings whose acts the broadcaster sped up by different amounts (nearly all of the runtime in long straight segments within 2 % of native, e.g. NBC's This Is Us and Brooklyn Nine-Nine), and a *corroborated rescue* for a similarity between describealaign's own 20 % mismatch line and the 30 % rescue floor over an almost perfect native-rate line — accepted only when the donor's filename names the same episode and the aligned audio track is English, because a straight line proves the timing, not the content. Otherwise the description is discarded and the original is untouched. As a safety net, the original is hardlinked into a backup before any overwrite (see `DESCRIBARR_BACKUP_*`), so a bad alignment is always recoverable.

---

## Requirements

- An AudioVault account (<https://audiovault.net/register>)
- Sonarr v3+ and/or Radarr v3+
- Docker (recommended) **or** Python 3.10+ with `ffmpeg` installed

---

## Setup — Docker (recommended)

This is the recommended approach if Sonarr/Radarr run in Docker, since the Custom Script executes inside each container and needs access to the same Python environment.

### 1. Create your `.env` file

```env
AUDIOVAULT_EMAIL=your@email.com
AUDIOVAULT_PASSWORD=yourpassword

# Minimum alignment score to keep a combined file (default: 65)
DESCRIBARR_MIN_SCORE=65
```

Keep this file private (`chmod 600 .env`).

### 2. Start the container

Copy `compose.example.yaml` to `compose.yaml`, adjust the volume paths to match what your Sonarr/Radarr containers mount (e.g. `/tv`, `/movies`), and set the network name to the one your arr containers share:

```bash
cp compose.example.yaml compose.yaml
# edit compose.yaml
docker compose up -d
```

The container starts a small webhook server on port 8686 (internal only — no need to expose it).

Verify your credentials loaded:

```bash
docker exec describarr describarr --test-auth
```

You should see `Login successful.`

### 3. Add the hook script to Sonarr and Radarr

Sonarr/Radarr execute Custom Scripts inside their own containers. The easiest place to drop a script that's already visible inside each container is their `config` volume (mounted at `/config`).

> The script uses `curl`, which the LinuxServer.io Sonarr/Radarr images already include. On a minimal image without it, install `curl` in that container first (or rewrite the hook using `wget`).

Create `/path/to/sonarr/config/describarr-hook.sh`:

```sh
#!/bin/sh
curl -sf --retry 5 --retry-all-errors --retry-delay 3 --max-time 30 \
  -X POST http://describarr:8686/hook \
  --data-urlencode "sonarr_eventtype=$sonarr_eventtype" \
  --data-urlencode "sonarr_series_title=$sonarr_series_title" \
  --data-urlencode "sonarr_series_year=$sonarr_series_year" \
  --data-urlencode "sonarr_episodefile_seasonnumber=$sonarr_episodefile_seasonnumber" \
  --data-urlencode "sonarr_episodefile_episodenumbers=$sonarr_episodefile_episodenumbers" \
  --data-urlencode "sonarr_episodefile_episodetitles=$sonarr_episodefile_episodetitles" \
  --data-urlencode "sonarr_episodefile_path=$sonarr_episodefile_path"
```

Sonarr and Radarr never re-run a failed Custom Script, so without the retry flags any import that lands while describarr restarts is lost. A stopped container makes the hostname fail to resolve, which plain `--retry` does not retry; `--retry-all-errors` does (curl ≥ 7.71). The episode title is optional: it lets describarr check that a donor names the episode it is describing.

Create `/path/to/radarr/config/describarr-hook.sh`:

```sh
#!/bin/sh
curl -sf --retry 5 --retry-all-errors --retry-delay 3 --max-time 30 \
  -X POST http://describarr:8686/hook \
  --data-urlencode "radarr_eventtype=$radarr_eventtype" \
  --data-urlencode "radarr_movie_title=$radarr_movie_title" \
  --data-urlencode "radarr_movie_year=$radarr_movie_year" \
  --data-urlencode "radarr_moviefile_path=$radarr_moviefile_path"
```

Make both executable:

```bash
chmod +x /path/to/sonarr/config/describarr-hook.sh
chmod +x /path/to/radarr/config/describarr-hook.sh
```

If you set `DESCRIBARR_API_KEY` (optional; the server is open by default, relying on docker-network isolation), add a matching header to both hook scripts so the mutating endpoints accept them:

```sh
curl -sf -X POST http://describarr:8686/hook \
  -H "X-Api-Key: your-secret" \
  --data-urlencode ...
```

### 4. Configure Sonarr and Radarr

In each app: **Settings → Connect → + → Custom Script**

| Field | Value |
|---|---|
| Name | `describarr` |
| On Import | ✅ |
| On Upgrade | ✅ |
| Path | `/config/describarr-hook.sh` |

Click **Test** — you should see a green tick. Then **Save**.

---

## Setup — bare metal / non-Docker

If Sonarr and Radarr run directly on the host (not in Docker), you can install describarr as a regular Python package and point the Custom Script at the binary.

### Install

```bash
pip install git+https://github.com/matalvernaz/describarr.git
```

This also pulls the `describealaign` engine straight from its git fork — it is **not published on PyPI**. Its optional GUI component (wxPython) isn't needed and can fail to build on some platforms; if the install trips on wxPython, install the engine `--no-deps` and add its runtime deps by hand:

```bash
pip install --no-deps "git+https://github.com/matalvernaz/describealaign.git@v2.1.9"
pip install "ffmpeg-python~=0.2.0" "static-ffmpeg~=3.0" \
    "numpy<3.0,>=1.21" "scipy~=1.10" "platformdirs~=4.2" \
    "natsort~=8.4.0" "sortedcontainers~=2.4.0" future
pip install --no-deps git+https://github.com/matalvernaz/describarr.git
```

### Configure

```bash
mkdir -p ~/.config/describarr
cp .env.example ~/.config/describarr/.env
nano ~/.config/describarr/.env
```

### Verify credentials

```bash
describarr --test-auth
```

### Set up in Sonarr / Radarr

**Settings → Connect → + → Custom Script**

| Field | Value |
|---|---|
| Name | `describarr` |
| On Import | ✅ |
| On Upgrade | ✅ |
| Path | output of `which describarr` (e.g. `/usr/local/bin/describarr`) |

---

## How it works

### TV episodes (Sonarr)

AudioVault distributes audio descriptions for TV shows as ZIP files containing one MP3 per episode. describarr:

1. Searches AudioVault for the series name and season number.
2. Downloads the season ZIP (cached — only downloaded once per season).
3. Extracts the ZIP and finds the right episode MP3 by episode number.
4. Runs describealaign on the video + MP3.
5. If the score is ≥ threshold, replaces the original file in-place with the combined version.

### Movies (Radarr)

AudioVault distributes movie audio descriptions as individual MP3 files. describarr searches by title, downloads the file, and runs the same alignment + scoring step.

### Caching

Downloaded season ZIPs are stored in `~/.cache/describarr/shows/<series>/` (or the Docker container's cache) and are never re-downloaded for the same season.

---

## Manual retry

If AudioVault didn't have an audio description when a file was imported, use the `/retry` endpoint on the running server. All retry requests return **202 Accepted immediately** — processing happens in the background and progress is visible in the container logs.

> **Base URL.** describarr publishes no host port by default (step 2), so the `http://localhost:8686` in the examples below works when you've `docker exec`'d into the container, or on a bare-metal install. From another container on the arr network, use `http://describarr:8686` instead (see the note at the end of this section).

All paths must be paths *inside the describarr container*, which are the same as what Sonarr/Radarr see (i.e. the same volume mounts).

`title`, `year`, `season`, and `episode` are inferred from the path layout — Sonarr's `/tv/<series>/Season N/<file.SxxExx.mkv>` and Radarr's `/movies/<Title (Year)>/<file>` are recognised automatically. Pass any of those parameters explicitly to override what was inferred (e.g. when the series folder name doesn't match AudioVault).

For TV, `year` is the year the series began and is what keeps a show apart from a same-titled reboot in the catalogue (`Season 1 (2005)` vs `Season 1 (2024)`). It is read from the series folder suffix (`Archer (2009)`), then from a Sonarr-style filename (`Show (2005) - S01E01 - …`), then from a `tvshow.nfo` in the series folder. If none of those carries it, the log says so — pass `year=` to pin it.

### Single TV episode

```
http://localhost:8686/retry?path=/tv/ted/Season%201/ted.S01E03.mkv
```

### Whole season

Point `dir=` at the season directory. describarr scans for video files and parses `SxxExx` from each filename automatically. Files that don't match that pattern are skipped — check the container logs if fewer episodes than expected are processed.

```
http://localhost:8686/retry?dir=/tv/ted/Season%201
```

A multi-episode file (`S02E12-E13`, `S03E18-E21`) is described with every covered episode's AD joined in order. If a source lacks one of the parts the file is left alone rather than published half described.

### Whole show

Point `dir=` at the show root. All seasons are queued and processed in order.

```
http://localhost:8686/retry?dir=/tv/ted
```

### What a directory scan skips

A rescan is meant to be cheap to repeat, so it queues only episodes that still need work. It skips:

- **Episodes whose file already carries an audio-description track.** The file is the authority, not the `.done` ledger — an episode described by any route is left alone even if the ledger never recorded it, and the ledger is repaired as the scan goes.
- **Episodes no source could cover**, for `DESCRIBARR_NOMATCH_TTL_DAYS` (default 30) after the miss. Otherwise every rescan re-runs the same fruitless search against every catalogue for a show none of them carry.

Both are self-correcting. An episode whose file loses its AD track (an arr re-grab replaced the merged file) is reprocessed and cleared from the ledger. A remembered miss is discarded as soon as the file changes, so an upgrade is always searched again, and it expires anyway because catalogues do gain titles. To override the memory for one scan, add `force=1`; a single-file `path=` retry always searches.

### Movie

```
http://localhost:8686/retry?path=/movies/Inception%20(2010)/Inception.2010.mkv
```

If the folder name doesn't include the year (or the AudioVault title differs), pass `title=` and `year=` explicitly.

---

> **Accessing the endpoint:** If describarr is not exposed on port 8686 externally, trigger retries from another container on the same Docker network:
> ```
> curl "http://describarr:8686/retry?dir=/tv/ted/Season%202"
> ```
> Or open a shell into the describarr container and use curl from there.

---

## Monitoring

`GET /status` is a status page showing the current job, the download-cap and queue counts, and a **recent-decisions** table: the last N accept / reject / skip / no-match decisions with their scores and reasons (`DESCRIBARR_HISTORY_SIZE`, default 50). It's a plain semantic page — headings and real tables — so it reads cleanly with a screen reader, and there's a `?format=json` view for programmatic polling. It replaces grepping container logs to see what happened overnight.

When a description was found but refused, the notification says so — *"Found an audio description, but it did not line up with this copy, so the file was left alone. (match score 21%)"* — rather than claiming none exists; *"No audio description found."* is kept for when no source had one. When an alignment can't be made, the Pushover notification carries the specific cause instead of a generic "errored" — e.g. *"AD is 22 min vs 45 min video — likely wrong/truncated episode"* or *"AD audio is 95% silence"* — so you know whether to swap the AD source or re-grab the video. (This relies on the failure diagnosis emitted by describealaign ≥ v2.1.9.)

When an alignment *is* published but the AD source turns out to be a different cut of the film (an unrated video against a theatrical description, say), the success notification says so and says where — *"Described. (AD source is a different cut: 75 s of the picture has no description at 1:19–1:45, 5:02–5:31)"* — because the inserted footage keeps its original soundtrack and you should expect stretches without narration. A stretch right after the title card is usually a recap the AD source omits. When most of the runtime is undescribed the wording changes to *"description covers only part of the picture: 23 min of 47 min has no description at 24:12–46:38"*, which is what a double episode aligned against a single episode's AD looks like. The same figures land in the `/status` decision log as `undescribed` and `dropped` seconds. Anything under 20 s is treated as ordinary seams and not mentioned.

A published file inherits the owner, group and permission bits of the file it replaces, and the sibling `.describarr_backup` folder and `.admerge.lock` file take the library folder's owner with group-writable modes. describarr runs as root in its container; without this every publish left root-owned entries behind, and a root-owned folder later blocks Sonarr/Radarr (uid 1000) from replacing the file on an upgrade.

---

## Troubleshooting

**"Login failed"** — Double-check your AudioVault credentials and run `describarr --test-auth`.

**"No AudioVault results"** — The show or movie may not have an audio description on AudioVault yet. Use the [manual retry](#manual-retry) endpoint once it becomes available.

**"Discarding … no trusted sync signal"** — The audio description didn't align confidently with the video (possibly a different version/cut), so describarr won't risk publishing a description that plays at the wrong moment. The original file is untouched.

**Green tick on Test but nothing happens on import** — Check that the hook script is executable and that the `describarr` container is on the same Docker network as Sonarr/Radarr.

---

## Adjusting the score threshold

Set `DESCRIBARR_MIN_SCORE=50` in your `.env` to accept lower-quality alignments. The describealaign documentation notes that scores below 20 % are likely mismatched files and scores above 90 % may indicate undescribed media.

---

## License

MIT
