FROM python:3.13-slim

# tzdata lets the TZ env var resolve to a real zone (the slim base is UTC-only),
# so the daily-limit reset and the midnight drain run on local time, not UTC.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg git tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install describealaign (Matt's fork of describealign). Carries the
# seam-crossfade fix, slope-stability metric, PAL/NTSC pre-resample,
# parsed-audio cache, and structured --json output. matplotlib is
# intentionally omitted: v2.1.2+ skips the PNG plot when matplotlib is
# missing, and describarr only consumes the .txt/.json reports.
RUN pip install --no-cache-dir \
    requests beautifulsoup4 python-dotenv \
    "ffmpeg-python~=0.2.0" "static-ffmpeg~=3.0" \
    "numpy<3.0,>=1.21" "scipy~=1.10" "platformdirs~=4.2" \
    "natsort~=8.4.0" "sortedcontainers~=2.4.0" future \
    && pip install --no-cache-dir --no-deps \
        "git+https://github.com/matalvernaz/describealaign.git@v2.2.2"

COPY . .
RUN pip install --no-cache-dir --no-deps .

EXPOSE 8686
CMD ["describarr", "serve"]
