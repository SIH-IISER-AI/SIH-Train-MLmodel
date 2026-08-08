FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install the shared package first
COPY shared/ /app/shared/
RUN pip install --no-cache-dir -e /app/shared

# Copy the specific service requirements and code
# (Change 'ai-engine/' to 'simulator/' for the simulator Dockerfile)
COPY ai-engine/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ai-engine/ .

CMD ["python", "main.py"]