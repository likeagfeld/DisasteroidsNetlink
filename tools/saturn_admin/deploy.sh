#!/usr/bin/env bash
# Deploys the Saturn unified admin portal + Disasteroids admin patch.
# Runs on the VM, expects /home/gary/staging_admin/ to be populated.
set -e

TS=$(date +%s)
STAGE=/home/gary/staging_admin

echo '=== 1. /opt/saturn-admin ==='
sudo mkdir -p /opt/saturn-admin
sudo cp "$STAGE/unified_admin.py" /opt/saturn-admin/unified_admin.py
sudo chown -R gary:gary /opt/saturn-admin
ls -l /opt/saturn-admin

echo '=== 2. systemd units ==='
sudo cp "$STAGE/saturn-admin.service" /etc/systemd/system/saturn-admin.service
sudo cp /etc/systemd/system/disasteroids.service "/etc/systemd/system/disasteroids.service.bak.$TS"
sudo cp "$STAGE/disasteroids.service" /etc/systemd/system/disasteroids.service

echo '=== 3. /home/gary/dserver.py ==='
cp /home/gary/dserver.py "/home/gary/dserver.py.bak.$TS"
cp "$STAGE/dserver.py" /home/gary/dserver.py

echo '=== 4. nginx config (backup + validate) ==='
sudo cp /etc/nginx/sites-enabled/saturncoup "/etc/nginx/sites-enabled/saturncoup.bak.$TS"
sudo cp "$STAGE/saturncoup-nginx" /etc/nginx/sites-enabled/saturncoup
if ! sudo nginx -t; then
  echo '!!! nginx -t failed; rolling back'
  sudo cp "/etc/nginx/sites-enabled/saturncoup.bak.$TS" /etc/nginx/sites-enabled/saturncoup
  exit 1
fi

echo '=== 5. daemon-reload + start services ==='
sudo systemctl daemon-reload
sudo systemctl enable saturn-admin
sudo systemctl restart saturn-admin
sleep 2
sudo systemctl restart disasteroids
sleep 2
sudo systemctl reload nginx

echo '=== 6. Service status ==='
for s in saturn-admin disasteroids nginx coup-server flock-server; do
  printf '%-16s %s\n' "$s" "$(systemctl is-active "$s")"
done

echo
echo '=== 7. Listening ports ==='
sudo ss -tlnp | grep -E ':(909[0-9]|482[0-9])' | sort

echo
echo '=== 8. Smoke test admin endpoints (localhost) ==='
for slug in coup flicky disasteroids; do
  code=$(curl -s -o /tmp/body.$$ -w '%{http_code}' -H 'X-Admin-Auth: nginx-verified' "http://127.0.0.1:9099/api/$slug/state" || echo err)
  bytes=$(wc -c < /tmp/body.$$ 2>/dev/null || echo 0)
  printf '  %-14s state  http=%s bytes=%s\n' "$slug" "$code" "$bytes"
done
rm -f /tmp/body.$$

echo
echo '=== 9. Admin page smoke test ==='
code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:9099/")
echo "  / -> $code"

echo
echo 'Done.'
