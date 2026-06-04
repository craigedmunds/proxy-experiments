FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir flask
COPY authz.py .
EXPOSE 9002
CMD ["python", "-u", "authz.py"]
