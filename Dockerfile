FROM eclipse-temurin:17-jre AS lavalink
WORKDIR /app
COPY Lavalink.jar .
COPY application.yml .

FROM python:3.11-slim
WORKDIR /app

# Copy bot files
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY --from=lavalink /app/Lavalink.jar .
COPY --from=lavalink /app/application.yml .

# Expose Lavalink port
EXPOSE 2333

# Run Lavalink + bot together
CMD java -jar Lavalink.jar & python bot.py
