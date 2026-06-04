FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir flask pyyaml
COPY app.py .
EXPOSE 5678
CMD ["python", "-u", "app.py"]
