#!/bin/sh
set -eu

for file in /credentials/username /credentials/password; do
  value=$(cat "$file")
  if [ "$(wc -c < "$file")" -ne "${#value}" ]; then
    echo "Proxy credentials must not contain trailing newlines" >&2
    exit 1
  fi
  case "$value" in
    ''|*[!a-zA-Z0-9._-]*)
      echo "Proxy credentials must be nonempty and contain only letters, digits, dot, underscore, or hyphen" >&2
      exit 1
      ;;
  esac
done
unset value

ip rule add priority 5260 to 10.42.0.0/16 lookup main
ip rule add priority 5261 to 10.43.0.10/32 lookup main
ip -6 rule add priority 5269 uidrange 10001-10001 unreachable
ip -6 rule add priority 5275 unreachable

iptables -P OUTPUT DROP
iptables -A OUTPUT -m owner --uid-owner 10001 -m conntrack --ctstate ESTABLISHED,RELATED --ctdir REPLY -j ACCEPT
for network in 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.168.0.0/16 224.0.0.0/4 240.0.0.0/4; do
  iptables -A OUTPUT -m owner --uid-owner 10001 -d "$network" -j REJECT
done
iptables -A OUTPUT -m owner --uid-owner 10001 -o tailscale0 -j ACCEPT
iptables -A OUTPUT -m owner --uid-owner 10001 -j REJECT
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A OUTPUT -o tailscale0 -j ACCEPT
iptables -A OUTPUT -d 10.43.0.10/32 -p udp --dport 53 -j ACCEPT
iptables -A OUTPUT -d 10.43.0.10/32 -p tcp --dport 53 -j ACCEPT
iptables -A OUTPUT -m mark --mark 0x80000/0xff0000 -j ACCEPT
iptables -A OUTPUT -m owner --uid-owner 0 -p tcp --dport 443 -j ACCEPT
