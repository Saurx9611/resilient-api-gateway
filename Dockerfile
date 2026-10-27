FROM golang:1.27.3-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY cmd/ ./cmd/
COPY internal/ ./internal/
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/gateway ./cmd/gateway

FROM alpine:3.22 AS runtime
RUN apk add --no-cache ca-certificates wget \
 && addgroup -S -g 10001 gateway \
 && adduser -S -u 10001 -G gateway -H -h /nonexistent gateway
USER 10001:10001
WORKDIR /app
COPY --from=build --chown=10001:10001 /out/gateway /usr/local/bin/gateway
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=2s --start-period=5s --retries=3 CMD wget -q -O /dev/null http://127.0.0.1:8080/readyz || exit 1
ENTRYPOINT ["/usr/local/bin/gateway"]
CMD ["-listen", "0.0.0.0:8080"]
