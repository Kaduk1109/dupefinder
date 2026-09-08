# Duplicate Image Finder — container image.
# Both the Flask review app and the scanner run inside this same image;
# app.py launches scanner.py as a subprocess of itself (same Python, same
# filesystem), so there's nothing extra to configure for that split.

FROM python:3.12-slim

WORKDIR /app

# Pillow ships manylinux wheels with libjpeg/libpng/etc. already bundled,
# so no extra system image libraries are needed for the formats in
# config.IMAGE_EXTENSIONS. tzdata keeps log/EXIF timestamp handling sane
# if you set TZ below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-optional.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Best-effort: HEIC/DNG support. If a wheel isn't available for this
# architecture, don't fail the whole build over it -- the app degrades
# gracefully (see hashing.py) and logs a clear message for those files.
RUN pip install --no-cache-dir -r requirements-optional.txt || true

COPY . .

# Where the SQLite DB + logs live -- mount this to a persistent volume
# (see docker-compose.yml) so scan results and review state survive
# container updates/rebuilds.
ENV DUPEFINDER_DATA_DIR=/data
RUN mkdir -p /data /photos

EXPOSE 5000

CMD ["python3", "app.py"]
