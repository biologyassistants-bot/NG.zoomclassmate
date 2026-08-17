FROM python:3.12-slim

WORKDIR /app

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
