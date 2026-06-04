#!/bin/sh
sleep 3
PERIOD=5
N=50
while true; do
  echo "[loadgen] sending $N requests over ${PERIOD}s"
  INTERVAL=$(awk "BEGIN {printf \"%.3f\", $PERIOD / $N}")
  i=0
  while [ $i -lt $N ]; do
    curl -s -o /dev/null -w "[loadgen] %{http_code}\n" http://envoy:8080/delay/$PERIOD &
    i=$((i + 1))
    sleep "$INTERVAL"
  done
  wait
  N=$((N * 2))
done
