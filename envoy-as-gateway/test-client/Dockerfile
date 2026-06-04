FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir requests
COPY test_client.py .
CMD ["python", "-u", "test_client.py"]
