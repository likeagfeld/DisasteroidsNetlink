#!/usr/bin/env bash
# Deploys ThreadingHTTPServer patches to all 3 game admins + the aggregator.
set -e
TS=$(date +%s)
STAGE=/home/gary/staging_admin

echo '=== Active connections on game ports before restart ==='
for p in 4821 4822 4823 4824; do
  n=$(sudo ss -tnp "sport = :$p" 2>/dev/null | tail -n +2 | wc -l)
  echo "  :$p  active=$n"
done
echo

echo '=== 1. Backup + install Flicky fserver.py ==='
sudo cp /opt/flock-server/fserver.py "/opt/flock-server/fserver.py.bak.$TS"
sudo cp "$STAGE/fserver.py" /opt/flock-server/fserver.py
sudo chown gary:gary /opt/flock-server/fserver.py

echo '=== 2. Backup + install Coup server.py ==='
sudo cp /opt/coup-server/tools/coup_server/server.py "/opt/coup-server/tools/coup_server/server.py.bak.$TS"
sudo cp "$STAGE/coup_server.py" /opt/coup-server/tools/coup_server/server.py
sudo chown gary:gary /opt/coup-server/tools/coup_server/server.py

echo '=== 3. Backup + install Disasteroids dserver.py ==='
cp /home/gary/dserver.py "/home/gary/dserver.py.bak.$TS"
cp "$STAGE/dserver.py" /home/gary/dserver.py

echo '=== 4. Install unified_admin.py ==='
sudo cp "$STAGE/unified_admin.py" /opt/saturn-admin/unified_admin.py
sudo chown gary:gary /opt/saturn-admin/unified_admin.py

echo '=== 5. Restart services ==='
sudo systemctl restart flock-server
sudo systemctl restart coup-server
sudo systemctl restart disasteroids
sudo systemctl restart saturn-admin
sleep 3

echo '=== 6. Service status ==='
for s in saturn-admin disasteroids flock-server coup-server; do
  printf '%-16s %s\n' "$s" "$(systemctl is-active "$s")"
done

echo
echo '=== 7. Direct admin curls ==='
for port_label in '9090 coup' '9091 flicky' '9092 disasteroids'; do
  port=${port_label%% *}
  label=${port_label##* }
  out=$(curl -s --max-time 3 -o /dev/null -w 'code=%{http_code} bytes=%{size_download} time=%{time_total}s' -H 'X-Admin-Auth: nginx-verified' "http://127.0.0.1:${port}/api/state")
  printf '  %-14s %s\n' "$label" "$out"
done

echo
echo '=== 8. Via aggregator ==='
for slug in coup flicky disasteroids; do
  out=$(curl -s --max-time 3 -o /dev/null -w 'code=%{http_code} bytes=%{size_download} time=%{time_total}s' "http://127.0.0.1:9099/api/$slug/state")
  printf '  %-14s %s\n' "$slug" "$out"
done

echo
echo '=== 9. Concurrency smoke: 20 parallel requests to disasteroids admin ==='
for i in $(seq 1 20); do
  curl -s --max-time 3 -o /dev/null -w '%{http_code}\n' -H 'X-Admin-Auth: nginx-verified' "http://127.0.0.1:9092/api/state" &
done | sort | uniq -c
wait

echo
echo 'Done.'
