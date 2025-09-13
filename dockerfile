# Use Python base image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies for audio + Lavalink runtime
RUN apt-get update && apt-get install -y \
    ffmpeg \
    openjdk-17-jre \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot files
COPY . .

# Expose Flask port
EXPOSE 8080

# Run the bot
CMD ["python", "bot.py"]
