FROM python:3.12-alpine
ARG APP_VERSION=1.5.0
ENV APP_VERSION=${APP_VERSION}
LABEL org.opencontainers.image.title="UR Vacancy Monitor" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.source="https://github.com/ladudu/ur-monitor"
WORKDIR /app
COPY app.py ur_check.py ./
RUN mkdir -p /data /config && addgroup -S app && adduser -S app -G app && chown -R app:app /data /config /app
USER app
EXPOSE 8080
VOLUME ["/data", "/config"]
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD wget -qO- http://127.0.0.1:8080/healthz || exit 1
CMD ["python", "-u", "app.py"]
