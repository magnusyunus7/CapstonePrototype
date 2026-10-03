FROM python:3.12-slim

WORKDIR /opt/simcc
COPY requirements.txt /opt/simcc/requirements.txt
RUN pip install --no-cache-dir -r /opt/simcc/requirements.txt

COPY app.py /opt/simcc/app.py
COPY static /opt/simcc/static

# topic_catalog.json lives in workspace/schema/ in the repo; in Docker we mount
# workspace/ as a volume so the live form always reads the latest verified catalog.
VOLUME /opt/simcc/workspace

EXPOSE 5005
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "5005"]