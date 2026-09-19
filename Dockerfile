# Bot and authenticated web panel run together (one process / one replica).
FROM python:3.12-slim-bookworm
COPY --from=denoland/deno:bin-2.9.7 /deno /usr/local/bin/deno
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 PORT=8080 DATA_DIR=/data \
    DENO_NO_UPDATE_CHECK=1 DENO_NO_PROMPT=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libopus0 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && python -c "import discord, nacl, davey, yt_dlp; print('voice dependencies available:', discord.__version__)" \
    && deno --version && ffmpeg -version > /dev/null
COPY . .
RUN mkdir -p /data
EXPOSE 8080
# Root inside the container is retained for compatibility with Railway-mounted volumes.
# On self-managed hosts, supply a non-root user after granting it access to /data.
CMD ["python", "main.py"]
