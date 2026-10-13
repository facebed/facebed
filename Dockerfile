FROM python:3.14-alpine AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip3.14 install -r ./requirements.txt

FROM python:3.14-alpine
WORKDIR /facebed
COPY facebed.py ./
COPY assets ./assets
RUN /bin/sh -c "echo '{}' > ./config.yaml"
COPY --from=builder /usr/local/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages
RUN adduser -D facebed
USER facebed
CMD ["python3.14", "./facebed.py", "-c", "./config.yaml"]
