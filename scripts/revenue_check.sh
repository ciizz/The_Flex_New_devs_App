#!/bin/bash
# Prints /dashboard/summary for every property as each tenant, then the cross-tenant leak sequence.
API=http://localhost:8000
login() { curl -s -X POST $API/api/v1/auth/login -H 'Content-Type: application/json' -d "{\"email\":\"$1\",\"password\":\"$2\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])'; }
TA=$(login sunset@propertyflow.com client_a_2024)
TB=$(login ocean@propertyflow.com client_b_2024)
echo "tokenA=${TA:0:20}... tokenB=${TB:0:20}..."
summary() { curl -s "$API/api/v1/dashboard/summary?property_id=$2" -H "Authorization: Bearer $1"; echo; }
flush() { docker-compose exec -T redis redis-cli FLUSHALL >/dev/null; }
echo "== tenant A (cache flushed first) =="; flush
for p in prop-001 prop-002 prop-003 prop-004 prop-005; do echo -n "A $p: "; summary $TA $p; done
echo "== tenant B (cache flushed first) =="; flush
for p in prop-001 prop-002 prop-003 prop-004 prop-005; do echo -n "B $p: "; summary $TB $p; done
echo "== leak sequence: flush, A views prop-001, then B views prop-001 =="; flush
echo -n "A prop-001: "; summary $TA prop-001
echo -n "B prop-001: "; summary $TB prop-001
echo "== redis keys =="; docker-compose exec -T redis redis-cli KEYS '*'
