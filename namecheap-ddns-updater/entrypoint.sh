#!/bin/bash

if [[ -z "$DDNS_PASSWORD" || -z "$DOMAIN_NAME" || -z "$HOSTNAME_ENTRIES" ]]; then
    echo "Please set DDNS_PASSWORD, DOMAIN_NAME and HOSTNAME_ENTRIES in .env"
    exit 1
fi

# Change IFS to comma for splitting
IFS=',' read -r -a hosts <<<"$HOSTNAME_ENTRIES"

while true; do
    publicip=$(curl -s https://checkip.amazonaws.com)
    echo "[$(date)] Public IP is $publicip"

    for host in "${hosts[@]}"; do
        trimmed_host=$(echo "$host" | xargs) # trim whitespace just in case
        echo "[$(date)] Updating $trimmed_host.$DOMAIN_NAME"
        curl -s "https://dynamicdns.park-your-domain.com/update?host=$trimmed_host&domain=$DOMAIN_NAME&password=$DDNS_PASSWORD&ip=$publicip"
    done

    sleep 600
done
