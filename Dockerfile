FROM mcr.microsoft.com/playwright/python:v1.52.0-jammy

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

CMD ["python", "bot.py"]
