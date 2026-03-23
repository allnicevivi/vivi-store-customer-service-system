FROM python:3.11-slim

WORKDIR /app/backend

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc librdkafka-dev curl ca-certificates \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/shared/           /app/backend/shared/
COPY backend/llm/              /app/backend/llm/
COPY backend/knowledge_base/   /app/backend/knowledge_base/
COPY backend/producer/         /app/backend/producer/
COPY backend/router_agent/     /app/backend/router_agent/
COPY backend/resolution_agent/ /app/backend/resolution_agent/
COPY backend/consumers/        /app/backend/consumers/
COPY backend/mock_apis/        /app/backend/mock_apis/
COPY backend/demo_app.py        /app/backend/demo_app.py
COPY frontend/                 /app/frontend/

# SERVICE 環境變數決定執行哪個服務，例如 router_agent/router_agent.py
CMD ["sh", "-c", "python /app/backend/${SERVICE}"]
