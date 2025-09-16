# Dockerfile (Render-ready) - starts Lavalink then bot via start.sh
FROM python:3.11-bullseye

ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Install system dependencies (ffmpeg, java, libsodium dev, build tools)
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
       ffmpeg \
       openjdk-17-jre-headless \
       libsodium-dev \
       build-essential \
       python3-dev \
       curl \
       ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# Download Lavalink.jar (official releases use this path)
RUN curl -L -o Lavalink.jar https://github.com/lavalink-devs/Lavalink/releases/latest/download/Lavalink.jar || true

# Ensure start script is executable
RUN chmod +x ./start.sh

# Expose ports
EXPOSE 2333 8080

# Start Lavalink (background) and then the bot (start.sh handles waiting)
CMD ["./start.sh"]
