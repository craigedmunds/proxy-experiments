FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir pytest requests
COPY test_client.py .
CMD ["pytest", "test_client.py", "-v", "--tb=short"]
