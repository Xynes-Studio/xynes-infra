# Source context is byte-identical to the pinned storage-service scanner.
FROM golang:1.27.2-bookworm@sha256:5cf287a799e6b94384bad13d16b14904c531f51ba65792237e122ce42b392f61 AS build
WORKDIR /src
COPY go.mod archive.go server.go archive_test.go gateway_test.go more_test.go server_test.go ./
RUN go test -list . ./... | grep -q '^Test' \
  && CGO_ENABLED=0 go test -count=1 -coverprofile=/tmp/scanner-coverage.out ./... \
  && awk 'NR>1 {split($1,f,":"); total[f[1]]+=$2; if($3>0) covered[f[1]]+=$2; count++} END {if(!count) exit 1; for(filename in total) {print filename,covered[filename]/total[filename]*100; if(covered[filename]/total[filename]<0.80) exit 1}}' /tmp/scanner-coverage.out \
  && CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o /scanner .

FROM clamav/clamav:1.5.2-debian@sha256:12a74204d5723b39de2dcb20ab679752ca5d30b1a1539646b2cd9f58a873e8c6 AS prod
COPY --from=build /scanner /usr/local/bin/xynes-archive-scanner
USER 1000:1000
ENTRYPOINT ["/usr/local/bin/xynes-archive-scanner"]
HEALTHCHECK NONE
