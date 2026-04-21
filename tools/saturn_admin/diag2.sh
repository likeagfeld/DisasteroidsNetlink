#!/usr/bin/env bash
set +e
echo '=== Coup service uptime ==='
systemctl show coup-server --property=ActiveEnterTimestamp,MainPID
echo
echo '=== curl :9090 (5s timeout) ==='
time curl -s -o /tmp/coup.out --max-time 5 -H 'X-Admin-Auth: nginx-verified' http://127.0.0.1:9090/api/state
echo "body: $(wc -c < /tmp/coup.out 2>/dev/null) bytes"
head -c 300 /tmp/coup.out 2>/dev/null
echo
echo '=== Coup :9090 socket backlog ==='
sudo ss -tlnp 'sport = :9090'
echo
echo '=== Coup :4823 (websocket) status — are there active players? ==='
sudo ss -tnp 'sport = :4823' | head -10
echo
echo '=== Coup process threads ==='
COUP_PID=$(systemctl show coup-server --property=MainPID --value)
echo "coup pid = $COUP_PID"
ps -L -p "$COUP_PID" -o pid,tid,psr,pcpu,stat,comm | head -20
