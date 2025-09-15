FROM python:3.11-slim

WORKDIR /app

# Prevent interactive prompts during apt install
ENV DEBIAN_FRONTEND=noninteractive

# Update and install dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    openjdk-17-jre-headless \
    curl \
    ca-certificates \
    gnupg \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot code
COPY . .

# Expose Flask port
EXPOSE 8080

CMD ["python", "bot.py"]
