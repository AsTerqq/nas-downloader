FROM python:3.11-slim

RUN apt-get update && apt-get install -y aria2 ffmpeg --no-install-recommends && rm -rf /var/lib/apt/lists/*

RUN pip install yt-dlp faster-whisper watchdog --no-cache-dir

# Pre-download Whisper tiny model into the image (~75 MB) so first use is instant
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('tiny', device='cpu', compute_type='int8')"

WORKDIR /app

COPY . .

CMD ["watchmedo", "auto-restart", "--directory=/app", "--pattern=*.py", "--recursive", "--", "python", "server.py"]
