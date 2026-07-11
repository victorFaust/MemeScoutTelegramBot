FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render sets PORT but we don't need it (worker, not web).
# Use /tmp for logs on ephemeral filesystem.
ENV LOG_FILE=/tmp/bot.log

CMD ["python", "-u", "main.py"]
