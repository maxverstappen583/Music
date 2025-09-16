# bot/Dockerfile
FROM python:3.11-bullseye

ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# System deps for PyNaCl build and ffmpeg for audio
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsodium-dev \
    build-essential \
    python3-dev \
    curl \
    ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot files
COPY . .

# Expose Flask port so Render health checks succeed
EXPOSE 8080

# Start the bot
CMD ["python", "musicbot_247_flask.py"]
