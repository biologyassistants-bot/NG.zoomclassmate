FROM python:3.12-slim

WORKDIR /app

# System deps: ffmpeg is needed to compress/split recording audio before
# sending it to the speech-to-text API (OpenAI Whisper caps uploads at 25 MB).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the whole app (backend + frontend + data)
COPY backend ./backend
COPY frontend ./frontend

WORKDIR /app/backend

# Cloud hosts inject $PORT; default to 8100 locally
ENV PORT=8100
EXPOSE 8100

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
