FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mta_feeds.py server.py ./
COPY data/ ./data/

# Fly.io / Render both set $PORT for you; default to 8000 for local runs.
ENV PORT=8000
EXPOSE 8000

CMD ["python", "server.py", "--http"]
