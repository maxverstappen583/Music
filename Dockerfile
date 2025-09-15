FROM python:3.11-bullseye

WORKDIR /app

ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies (Java for Lavalink, ffmpeg for audio)
RUN apt-get update && apt-get upgrade -y && apt-get install -y \
    ffmpeg \
    openjdk-17-jre \
    curl \
    ca-certificates \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot and Lavalink config
COPY . .

# Download Lavalink jar
RUN curl -L -o Lavalink.jar https://github.com/lavalink-devs/Lavalink/releases/latest/download/Lavalink.jar

# Expose ports (2333 for Lavalink, 8080 for Flask)
EXPOSE 2333
EXPOSE 8080

# Start both Lavalink + bot
CMD java -jar Lavalink.jar & python bot.py
