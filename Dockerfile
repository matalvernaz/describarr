FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install describealign from Matt's fork: v2.1.0 carries the seam-crossfade
# fix that materially changes the audible quality of broadcast-source AD
# aligned against streaming-source video (Buffy/Charmed and any PAL→NTSC
# content with commercial breaks in the AD).
RUN pip install --no-cache-dir \
    requests beautifulsoup4 python-dotenv \
    "ffmpeg-python~=0.2.0" "static-ffmpeg~=3.0" "matplotlib~=3.9" \
    "numpy<3.0,>=1.21" "scipy~=1.10" "platformdirs~=4.2" \
    "natsort~=8.4.0" "sortedcontainers~=2.4.0" future \
    && pip install --no-cache-dir --no-deps \
        "git+https://github.com/matalvernaz/describealign.git@v2.1.1"

COPY . .
RUN pip install --no-cache-dir --no-deps .

EXPOSE 8686
CMD ["describarr", "serve"]
