#!/usr/bin/env bash
# Run under: sudo unshare --net --mount --propagation private bash ...
# All firewall and network operations stay inside the isolated namespace.
set -euo pipefail
work_dir="$(mktemp -d)"
lan_ns="pp-lan-$$"; wan_ns="pp-wan-$$"
server_pid=''
cleanup() {
  if [[ -n "$server_pid" ]]; then kill "$server_pid" 2>/dev/null || true; fi
  ip netns del "$lan_ns" 2>/dev/null || true
  ip netns del "$wan_ns" 2>/dev/null || true
  rm -rf -- "$work_dir"
}
trap cleanup EXIT
ip link set lo up
ip netns add "$lan_ns"
ip netns add "$wan_ns"
for name in lan wan; do
  namespace="$lan_ns"; prefix='192.168.50'
  if [[ "$name" == wan ]]; then namespace="$wan_ns"; prefix='198.51.100'; fi
  ip link add "${name}0" type veth peer name "${name}1"
  ip link set "${name}1" netns "$namespace"
  ip address add "$prefix.1/24" dev "${name}0"
  ip link set "${name}0" up
  ip -n "$namespace" address add "$prefix.2/24" dev "${name}1"
  ip -n "$namespace" link set "${name}1" up
  ip -n "$namespace" link set lo up
done
nft -f - <<'NFT'
table inet existing_user_firewall {
 chain input { type filter hook input priority 0; policy accept; }
}
NFT
python3 - "$work_dir" <<'PY'
import pathlib,sys
sys.path.insert(0,'companion')
from network_guard import apply_guard
apply_guard({'port':32187}, scopes=[('lan0','192.168.50.0/24')], root=pathlib.Path(sys.argv[1]))
apply_guard({'port':32187}, scopes=[('lan0','192.168.50.0/24')], root=pathlib.Path(sys.argv[1]))
PY
nft list table inet existing_user_firewall >/dev/null
python3 -u - "$work_dir/ready" <<'PY' &
import pathlib,socket,sys,threading
servers=[]
for port in [32187,18080]:
    sock=socket.socket(); sock.bind(('0.0.0.0',port)); sock.listen(); servers.append(sock)
    def respond(server):
        while True:
            connection,_=server.accept(); connection.sendall(b'hello'); connection.close()
    threading.Thread(target=respond,args=(sock,),daemon=True).start()
pathlib.Path(sys.argv[1]).touch()
threading.Event().wait()
PY
server_pid="$!"
for _ in {1..50}; do [[ -f "$work_dir/ready" ]] && break; sleep 0.1; done
test -f "$work_dir/ready"
ip netns exec "$lan_ns" python3 - <<'PY'
import socket
with socket.create_connection(('192.168.50.1',32187),timeout=2) as connection:
    assert connection.recv(5)==b'hello'
print('The local subnet can reach the companion port.')
PY
ip netns exec "$wan_ns" python3 - <<'PY'
import socket
try:
    connection=socket.create_connection(('198.51.100.1',32187),timeout=2)
except OSError:
    pass
else:
    connection.close(); raise SystemExit('Outside-network traffic was allowed')
with socket.create_connection(('198.51.100.1',18080),timeout=2) as connection:
    assert connection.recv(5)==b'hello'
print('Outside-network traffic to the companion is blocked; unrelated ports remain reachable.')
PY
# Exercise actual broadcasts on 1990 and multicast membership on 2021.
python3 -u - "$work_dir" <<'PY' &
import json,pathlib,sys,threading,time
sys.path.insert(0,'companion')
from bambu_monitor import PrinterMonitor
from network_guard import LANPolicy
root=pathlib.Path(sys.argv[1])
monitor=PrinterMonitor(root/'printer.json', LANPolicy(root/'run/print-monitor-network/scopes.json'), threading.Event())
(root/'discovery-started').touch()
found=monitor.discover()
assert {item['serial'] for item in found} == {'BROADCAST123', 'MULTICAST123'}, found
print('Real LAN broadcast and multicast printer discovery passed on both ports.')
PY
discovery_pid="$!"
for _ in {1..50}; do [[ -f "$work_dir/discovery-started" ]] && break; sleep 0.1; done
ip netns exec "$lan_ns" python3 - <<'PY'
import socket,time
with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sender:
    sender.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1)
    sender.setsockopt(socket.IPPROTO_IP,socket.IP_MULTICAST_IF,socket.inet_aton('192.168.50.2'))
    sender.setsockopt(socket.IPPROTO_IP,socket.IP_MULTICAST_TTL,1)
    sender.bind(('192.168.50.2',0))
    # Repeat briefly to avoid depending on the listener's scheduling.
    for _ in range(5):
        for serial,destination in [('BROADCAST123',('192.168.50.255',1990)),('MULTICAST123',('239.255.255.250',2021))]:
            payload=f'NOTIFY * HTTP/1.1\r\nNT: urn:bambulab-com:device:3dprinter:1\r\nLocation: 192.168.50.2\r\nUSN: {serial}\r\n\r\n'.encode()
            sender.sendto(payload,destination)
        time.sleep(0.2)
PY
wait "$discovery_pid"
