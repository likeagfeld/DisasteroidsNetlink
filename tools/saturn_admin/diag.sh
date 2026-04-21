#!/usr/bin/env bash
set +e
echo '=== direct curl to coup admin :9090 ==='
curl -sv --max-time 3 -H 'X-Admin-Auth: nginx-verified' http://127.0.0.1:9090/api/state 2>&1 | head -40
echo
echo '=== direct curl to flicky admin :9091 ==='
curl -s -o /dev/null -w 'code=%{http_code} bytes=%{size_download}\n' --max-time 3 -H 'X-Admin-Auth: nginx-verified' http://127.0.0.1:9091/api/state
echo
echo '=== direct curl to disasteroids admin :9092 ==='
curl -s -o /dev/null -w 'code=%{http_code} bytes=%{size_download}\n' --max-time 3 -H 'X-Admin-Auth: nginx-verified' http://127.0.0.1:9092/api/state
echo
echo '=== sites-enabled listing ==='
sudo ls -la /etc/nginx/sites-enabled/
echo
echo '=== grep for saturncoup.duckdns.org in all nginx configs ==='
sudo grep -rn 'saturncoup.duckdns.org' /etc/nginx/ 2>/dev/null | head -20
echo
echo '=== external /admin/ (no auth) ==='
curl -s -o /dev/null -w 'code=%{http_code}\n' https://saturncoup.duckdns.org/admin/
