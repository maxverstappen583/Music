#!/usr/bin/env bash
set -e

# Start Lavalink in background (if Lavalink.jar exists)
if [ -f "./Lavalink.jar" ]; then
  echo "Starting Lavalink..."
  java -jar Lavalink.jar &

  # Wait for Lavalink to accept connections
  TIMEOUT=60
  echo "Waiting for Lavalink on 127.0.0.1:2333 (timeout ${TIMEOUT}s)..."
  for i in $(seq 1 $TIMEOUT); do
    # use bash /dev/tcp trick
    if (echo > /dev/tcp/127.0.0.1/2333) >/dev/null 2>&1; then
      echo "Lavalink is up."
      break
    fi
    sleep 1
  done
else
  echo "Lavalink.jar not found; starting bot only (expect playback to fail)."
fi

# Start the bot
echo "Starting Python bot..."
python musicbot_247_flask.py
