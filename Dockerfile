FROM python:3.12-slim

WORKDIR /app

# Alleen wat nodig is
COPY requirements-monitor.txt .
RUN pip install --no-cache-dir -r requirements-monitor.txt

COPY scripts/vlucht_monitor.py .

# Telegram credentials via fly secrets (omgevingsvariabelen)
ENV TELEGRAM_TOKEN=""
ENV TELEGRAM_CHAT_ID=""

CMD ["python", "-u", "vlucht_monitor.py"]
