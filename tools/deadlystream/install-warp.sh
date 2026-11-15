#!/usr/bin/env bash
set -euo pipefail
if [ "${PRESERVATION_RUNNER_KIND:-}" = self-hosted ]; then
  curl --silent --show-error --fail --max-time 20 --proxy socks5h://127.0.0.1:40000 https://www.cloudflare.com/cdn-cgi/trace | grep -q '^warp=on$'
  printf 'Verified isolated cloud-side WARP relay.\n'
  exit 0
fi
curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg | sudo gpg --yes --dearmor --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
printf 'deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ noble main\n' | sudo tee /etc/apt/sources.list.d/cloudflare-client.list >/dev/null
sudo apt-get update -qq
sudo apt-get install -y -qq cloudflare-warp age
sudo systemctl start warp-svc
warp-cli --accept-tos registration new
warp-cli --accept-tos mode proxy
warp-cli --accept-tos proxy port 40000
warp-cli --accept-tos connect
for attempt in $(seq 1 30); do
  if curl --silent --show-error --fail --max-time 10 --proxy socks5h://127.0.0.1:40000 https://www.cloudflare.com/cdn-cgi/trace | grep -q '^warp=on$'; then
    printf 'Verified cloud-side WARP proxy; no local tunnel.\n'
    exit 0
  fi
  sleep 2
done
printf 'Cloud-side WARP proxy unavailable; source requests remain disabled.\n' >&2
exit 1
