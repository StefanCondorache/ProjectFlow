FROM python:3.12-slim
WORKDIR /app
COPY . .
ENTRYPOINT ["python"]
CMD ["-m", "shop", "--config", "prod.json"]
