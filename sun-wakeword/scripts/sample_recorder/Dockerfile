FROM python:3.12-slim
RUN pip install --no-cache-dir "wyoming==1.5.4"
COPY recorder.py /app/recorder.py
WORKDIR /app
EXPOSE 10500
ENTRYPOINT ["python", "recorder.py", "--uri", "tcp://0.0.0.0:10500", "--out-dir", "/data"]
