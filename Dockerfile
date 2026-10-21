FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    IMAGEIO_FFMPEG_EXE=/usr/bin/ffmpeg

# ffmpeg (with rubberband for pitch-preserving slowdown), LibreOffice for PPTX -> PDF,
# and fonts: Carlito/Caladea are metric-compatible with Calibri/Cambria, Noto covers Indic scripts.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libreoffice-impress \
        fonts-liberation fonts-dejavu-core \
        fonts-crosextra-carlito fonts-crosextra-caladea \
        fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
