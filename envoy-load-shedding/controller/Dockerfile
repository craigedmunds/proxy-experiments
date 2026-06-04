FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir flask requests prometheus-client
COPY controller.py .
EXPOSE 8081
CMD ["python", "controller.py"]
