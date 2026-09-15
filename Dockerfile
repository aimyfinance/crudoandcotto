FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p data/backups data/exports
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "bot.main"]
