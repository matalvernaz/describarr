FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install describealaign (Matt's fork of describealign). Carries the
# seam-crossfade fix, slope-stability metric, PAL/NTSC pre-resample,
# parsed-audio cache, and structured --json output. Renamed at v3.0.0.
RUN pip install --no-cache-dir \
    requests beautifulsoup4 python-dotenv \
    "ffmpeg-python~=0.2.0" "static-ffmpeg~=3.0" "matplotlib~=3.9" \
    "numpy<3.0,>=1.21" "scipy~=1.10" "platformdirs~=4.2" \
    "natsort~=8.4.0" "sortedcontainers~=2.4.0" future \
    && pip install --no-cache-dir --no-deps \
        "git+https://github.com/matalvernaz/describealaign.git@v2.0.10"

COPY . .
RUN pip install --no-cache-dir --no-deps .

EXPOSE 8686
CMD ["describarr", "serve"]
