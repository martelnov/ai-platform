FROM golang:1.25-bookworm AS build

WORKDIR /src

COPY main.go .

RUN CGO_ENABLED=0 GOOS=linux go build \
    -trimpath \
    -ldflags="-s -w" \
    -o /gateway \
    main.go


FROM scratch

COPY --from=build /gateway /gateway

USER 65532:65532

ENTRYPOINT ["/gateway"]
