# The official collector image is distroless: no shell and no wget, so Docker cannot run a
# health check inside it. Add a static busybox binary, used only by the health check.
FROM busybox:1.37.0-musl AS busybox

FROM otel/opentelemetry-collector:0.163.2
COPY --from=busybox /bin/busybox /busybox
COPY infra/otel/collector.yaml /etc/otelcol/config.yaml
